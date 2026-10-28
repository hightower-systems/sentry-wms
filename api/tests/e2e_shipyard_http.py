"""Run the shipyard OCR workflow against a real loopback HTTP server.

This is intentionally a standalone script rather than a pytest test: pytest's
autouse transaction fixture binds the application to one SQLAlchemy connection,
which is not a realistic multi-threaded HTTP setup. The script still refuses to
run unless TEST_DATABASE_URL points at the dedicated test database.

Example inside the API container:

    TEST_DATABASE_URL=postgresql://.../sentry_test \
      python tests/e2e_shipyard_http.py
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from urllib.parse import urlsplit

import psycopg2
import requests
from werkzeug.serving import make_server


def _test_database_url() -> str:
    test_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not test_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    database_name = urlsplit(test_url).path.lstrip("/")
    if database_name != "sentry_test":
        raise RuntimeError("Refusing to run destructive E2E setup outside sentry_test")
    current_url = (os.environ.get("DATABASE_URL") or "").strip()
    if current_url and current_url == test_url:
        raise RuntimeError("TEST_DATABASE_URL must differ from DATABASE_URL")
    return test_url


TEST_DATABASE_URL = _test_database_url()
os.environ["FLASK_ENV"] = "development"
os.environ["SENTRY_ALLOW_INTERNAL_WEBHOOKS"] = "false"
os.environ["SENTRY_ALLOW_HTTP_WEBHOOKS"] = "false"

# conftest performs the same test-DB routing and deterministic admin seeding
# used by the regular suite. Importing it here deliberately keeps E2E setup and
# credentials identical to the tested environment.
import conftest
from shipyard.helpers import (
    PNG_BYTES,
    outbound_manifest_bytes,
    single_line_manifest_bytes,
)

from app import create_app
from services.rate_limit import limiter
from services.shipyard import (
    outbound_turnover_service,
    receipt_service,
)
from services.shipyard.errors import OcrProviderError
from services.shipyard.label_ocr import LabelOcrResult


def _ocr_result(fields: dict) -> LabelOcrResult:
    return LabelOcrResult(
        provider="e2e-stub",
        raw_text="\n".join(f"{key}: {value}" for key, value in fields.items()),
        raw_lines=[f"{key}: {value}" for key, value in fields.items()],
        fields=fields,
        confidence=0.99,
    )


def _label_files(file_name: str) -> dict:
    return {
        "file": (
            file_name,
            BytesIO(PNG_BYTES),
            "image/png",
        )
    }


def _raise_ocr_provider(_content):
    raise OcrProviderError("E2E simulated OCR timeout")


def _json(response: requests.Response, expected_status: int) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {"body": response.text[:1000]}
    if response.status_code != expected_status:
        raise AssertionError(
            f"{response.request.method} {response.request.url} "
            f"returned {response.status_code}: {payload}"
        )
    return payload


def _login_session(api_base: str, username: str, password: str) -> requests.Session:
    session = requests.Session()
    login = _json(
        session.post(
            f"{api_base}/auth/login",
            json={"username": username, "password": password},
            timeout=10,
        ),
        200,
    )
    session.headers["Authorization"] = f"Bearer {login['token']}"
    return session


def _create_role_session(
    admin_session: requests.Session,
    api_base: str,
    *,
    run_id: str,
    role: str,
    page_keys: list[str],
    warehouse_ids: list[int],
) -> requests.Session:
    username = f"e2e_{role.lower()}_{run_id.lower()}_{uuid.uuid4().hex[:4]}"
    password = "ShipyardE2E2026!"
    created = _json(
        admin_session.post(
            f"{api_base}/admin/users",
            json={
                "username": username,
                "password": password,
                "full_name": f"E2E {role}",
                "role": role,
                "warehouse_ids": warehouse_ids,
            },
            timeout=10,
        ),
        201,
    )
    _json(
        admin_session.put(
            f"{api_base}/admin/users/{created['user_id']}/permissions",
            json={"page_keys": page_keys},
            timeout=10,
        ),
        200,
    )
    return _login_session(api_base, username, password)


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _parallel_json_posts(
    api_base: str,
    session: requests.Session,
    path: str,
    payload: dict,
    *,
    count: int | None = None,
    max_workers: int | None = None,
) -> list[requests.Response]:
    count = count or _positive_env_int("SHIPYARD_E2E_REQUEST_COUNT", 50)
    max_workers = max_workers or _positive_env_int(
        "SHIPYARD_E2E_MAX_WORKERS",
        10,
    )
    authorization = session.headers["Authorization"]
    expected_header_hash = sha256(authorization.encode()).hexdigest()[:16]

    def post_once():
        response = requests.post(
            f"{api_base}{path}",
            json=payload,
            headers={"Authorization": authorization},
            timeout=30,
        )
        return response

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(post_once) for _ in range(count)]
        responses = [future.result() for future in futures]
    failures = [response for response in responses if response.status_code == 401]
    if failures:
        observed_header_hashes = {
            sha256(
                (response.request.headers.get("Authorization") or "").encode()
            ).hexdigest()[:16]
            for response in failures
        }
        sequential_check = session.get(f"{api_base}/auth/me", timeout=10)
        raise AssertionError(
            {
                "expected_header_hash": expected_header_hash,
                "observed_header_hashes": sorted(observed_header_hashes),
                "sequential_auth_status": sequential_check.status_code,
                "failure_count": len(failures),
                "failure_bodies": [response.text[:200] for response in failures[:3]],
            }
        )
    return responses


def _first_bin_id() -> int:
    connection = psycopg2.connect(TEST_DATABASE_URL)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT bin_id
              FROM bins
             WHERE warehouse_id = 1
             ORDER BY bin_id
             LIMIT 1
            """
        )
        return cursor.fetchone()[0]
    finally:
        connection.close()


def _set_expected_pallet_count(ticket_id: str, count: int) -> None:
    connection = psycopg2.connect(TEST_DATABASE_URL)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE shipyard_cargo_tickets
               SET expected_pallet_count = %s
             WHERE ticket_id = %s
            """,
            (count, ticket_id),
        )
        connection.commit()
    finally:
        connection.close()


def _database_evidence(ticket_id: str, confirmation_id: str) -> dict:
    connection = psycopg2.connect(TEST_DATABASE_URL)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT on_hand_quantity, status
              FROM shipyard_cargo_tickets
             WHERE ticket_id = %s
            """,
            (ticket_id,),
        )
        on_hand_quantity, ticket_status = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*),
                   COUNT(label_recognition_id),
                   COUNT(photo_id)
              FROM shipyard_outbound_confirmation_pallets
             WHERE confirmation_id = %s
            """,
            (confirmation_id,),
        )
        pallet_count, recognition_links, photo_links = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(quantity_delta), 0)
              FROM shipyard_stock_ledger
             WHERE ticket_id = %s
               AND source_type = 'OUTBOUND'
            """,
            (ticket_id,),
        )
        ledger_count, outbound_delta = cursor.fetchone()
        return {
            "on_hand_quantity": str(on_hand_quantity),
            "ticket_status": ticket_status,
            "confirmation_pallets": pallet_count,
            "recognition_links": recognition_links,
            "photo_links": photo_links,
            "outbound_ledger_count": ledger_count,
            "outbound_delta": str(outbound_delta),
        }
    finally:
        connection.close()


def _database_invariants() -> dict:
    queries = {
        "ticket_ledger_balance": """
            SELECT t.ticket_id::text
              FROM shipyard_cargo_tickets t
              LEFT JOIN shipyard_stock_ledger l
                ON l.ticket_id = t.ticket_id
             GROUP BY t.ticket_id, t.on_hand_quantity
            HAVING t.on_hand_quantity <> COALESCE(SUM(l.quantity_delta), 0)
        """,
        "ticket_receipt_total": """
            SELECT t.ticket_id::text
              FROM shipyard_cargo_tickets t
              LEFT JOIN shipyard_receipts r
                ON r.ticket_id = t.ticket_id
             GROUP BY t.ticket_id, t.received_quantity
            HAVING t.received_quantity <> COALESCE(SUM(r.received_quantity), 0)
        """,
        "outbound_confirmation_ledger": """
            SELECT c.confirmation_id::text
              FROM shipyard_outbound_confirmations c
              LEFT JOIN shipyard_stock_ledger l
                ON l.source_type = 'OUTBOUND'
               AND l.source_id = c.confirmation_id
             GROUP BY c.confirmation_id, c.quantity,
                      c.previous_on_hand, c.balance_after
            HAVING COUNT(l.source_id) <> 1
                OR COALESCE(MIN(l.quantity_delta), 0) <> -c.quantity
                OR c.previous_on_hand - c.quantity <> c.balance_after
                OR c.balance_after <> 0
        """,
        "outbound_confirmation_pallets": """
            SELECT c.confirmation_id::text
              FROM shipyard_outbound_confirmations c
              JOIN shipyard_outbound_tasks t
                ON t.task_id = c.task_id
              LEFT JOIN shipyard_outbound_confirmation_pallets cp
                ON cp.confirmation_id = c.confirmation_id
              LEFT JOIN shipyard_label_recognitions lr
                ON lr.recognition_id = cp.label_recognition_id
              LEFT JOIN shipyard_photos ph
                ON ph.photo_id = cp.photo_id
             GROUP BY c.confirmation_id, c.task_id, c.ticket_id,
                      t.expected_pallet_count
            HAVING COUNT(cp.pallet_id) <> t.expected_pallet_count
                OR COUNT(DISTINCT cp.tag_suffix) <> t.expected_pallet_count
                OR COUNT(DISTINCT cp.photo_id) <> t.expected_pallet_count
                OR COUNT(*) FILTER (
                    WHERE lr.recognition_id IS NULL
                       OR lr.task_id <> c.task_id
                       OR lr.ticket_id <> c.ticket_id
                       OR lr.photo_id <> cp.photo_id
                   ) > 0
                OR COUNT(*) FILTER (
                    WHERE ph.outbound_confirmation_id <> c.confirmation_id
                   ) > 0
        """,
        "outbound_task_ticket_pallets": """
            SELECT t.task_id::text
              FROM shipyard_outbound_tasks t
              JOIN shipyard_cargo_tickets c
                ON c.ticket_id = t.ticket_id
             WHERE t.status = 'OUTBOUND'
               AND (
                   c.status <> 'OUTBOUND'
                   OR c.on_hand_quantity <> 0
                   OR EXISTS (
                       SELECT 1
                         FROM shipyard_pallet_units p
                        WHERE p.ticket_id = t.ticket_id
                          AND p.status <> 'OUTBOUND'
                   )
               )
        """,
        "loaded_order_loading": """
            SELECT o.outbound_order_id::text
              FROM shipyard_outbound_orders o
              LEFT JOIN shipyard_outbound_loading_confirmations l
                ON l.outbound_order_id = o.outbound_order_id
             WHERE o.status = 'LOADED'
             GROUP BY o.outbound_order_id
            HAVING COUNT(l.loading_confirmation_id) <> 1
                OR EXISTS (
                    SELECT 1
                      FROM shipyard_outbound_tasks t
                     WHERE t.outbound_order_id = o.outbound_order_id
                       AND t.status <> 'OUTBOUND'
                )
        """,
    }
    violations = {}
    connection = psycopg2.connect(TEST_DATABASE_URL)
    try:
        cursor = connection.cursor()
        for name, query in queries.items():
            cursor.execute(query)
            rows = [row[0] for row in cursor.fetchall()]
            if rows:
                violations[name] = rows
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM shipyard_cargo_tickets),
                (SELECT COUNT(*) FROM shipyard_stock_ledger),
                (SELECT COUNT(*) FROM shipyard_outbound_confirmations),
                (
                    SELECT COUNT(*)
                      FROM shipyard_outbound_confirmation_pallets
                )
            """
        )
        ticket_count, ledger_count, confirmation_count, pallet_count = cursor.fetchone()
        return {
            "passed": not violations,
            "violations": violations,
            "ticket_count": ticket_count,
            "ledger_count": ledger_count,
            "confirmation_count": confirmation_count,
            "confirmation_pallet_count": pallet_count,
        }
    finally:
        connection.close()


class _ServerThread:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="shipyard-e2e-http",
            daemon=True,
        )

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/api"

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.thread.join(timeout=5)


def run() -> dict:
    conftest._seed_database()
    app = create_app()
    app.config["TESTING"] = False
    limiter._storage.reset()

    run_id = uuid.uuid4().hex[:10].upper()
    ship_no = f"HE2E{run_id[:6]}"
    material_code = f"MAT-E2E-{run_id}"
    item_name = "E2E 船用支架"
    suffixes = ("1-1", "1-2")

    inbound_ocr = [
        _ocr_result(
            {
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": "D-001",
                "material_code": material_code,
                "item_name": item_name,
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffix,
                "label_quantity": "4",
            }
        )
        for suffix in suffixes
    ]
    outbound_ocr = [
        _ocr_result(
            {
                "material_code": f"WRONG-{material_code}",
                "item_name": f"错误-{item_name}",
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffixes[0],
                "label_quantity": "4",
            }
        ),
        _ocr_result(
            {
                "material_code": material_code,
                "item_name": item_name,
                "batch_no": f"{ship_no}-BATCH-1",
                "label_quantity": "4",
            }
        ),
        _ocr_result(
            {
                "material_code": material_code,
                "item_name": item_name,
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffixes[1],
                "label_quantity": "4",
            }
        ),
    ]
    receipt_service.recognize_label_image = lambda _content: inbound_ocr.pop(0)
    outbound_turnover_service.recognize_label_image = lambda _content: outbound_ocr.pop(
        0
    )

    steps = []
    try:
        with _ServerThread(app) as api_base:
            session = _login_session(api_base, "admin", "admin")
            steps.append("管理员通过真实 HTTP 登录")

            _json(
                session.post(
                    f"{api_base}/shipyard/bindings",
                    json={
                        "ship_no": ship_no,
                        "engineering_no": ship_no,
                        "production_drawing_no": "D-001",
                        "manufacturer_name": "E2E 测试配件厂",
                    },
                    timeout=10,
                ),
                201,
            )
            _json(
                session.post(
                    f"{api_base}/shipyard/zone-mappings",
                    json={
                        "warehouse_id": 1,
                        "bin_id": _first_bin_id(),
                        "ship_no": ship_no,
                        "is_primary": True,
                    },
                    timeout=10,
                ),
                201,
            )
            steps.append("创建船号绑定和唯一存放区域")

            inbound_preview = _json(
                session.post(
                    f"{api_base}/shipyard/inbound/manifests/preview",
                    data={
                        "warehouse_id": "1",
                        "manifest_no": f"IN-E2E-{run_id}",
                    },
                    files={
                        "file": (
                            "inbound-e2e.xlsx",
                            BytesIO(
                                single_line_manifest_bytes(
                                    ship_no,
                                    quantity=4,
                                    drawing="D-001",
                                    manufacturer="E2E 测试配件厂",
                                    material_code=material_code,
                                    item_name=item_name,
                                    item_spec="S-E2E",
                                )
                            ),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    timeout=30,
                ),
                201,
            )
            manifest_id = inbound_preview["manifest"]["manifest_id"]
            inbound_confirmed = _json(
                session.post(
                    f"{api_base}/shipyard/inbound/manifests/{manifest_id}/confirm",
                    json={},
                    timeout=20,
                ),
                200,
            )
            ticket = inbound_confirmed["tickets"][0]
            _set_expected_pallet_count(ticket["ticket_id"], 2)
            steps.append("导入并确认到货清单，生成两张托盘任务")

            pallets = []
            receipt_photo_ids = []
            for suffix in suffixes:
                recognition = _json(
                    session.post(
                        f"{api_base}/shipyard/inbound/tickets/"
                        f"{ticket['ticket_id']}/label-recognition",
                        files={
                            "file": (
                                f"inbound-{suffix}.png",
                                BytesIO(PNG_BYTES),
                                "image/png",
                            )
                        },
                        timeout=10,
                    ),
                    201,
                )
                if not recognition["matched"]:
                    raise AssertionError(recognition)
                if recognition["recognized"]["tag_suffix"] != suffix:
                    raise AssertionError("入货 OCR 后缀不一致")
                receipt_photo_ids.append(recognition["photo"]["photo_id"])
                pallets.append(
                    {
                        "tag_suffix": suffix,
                        "label_recognition_id": recognition["recognition"][
                            "recognition_id"
                        ],
                        "photo_id": recognition["photo"]["photo_id"],
                    }
                )

            receipt_key = f"receipt-e2e-{run_id}"
            receipt_payload = {
                "idempotency_key": receipt_key,
                "client_device_id": f"wx-e2e-{run_id}",
                "received_quantity": 4,
                "pallets": pallets,
            }
            _json(
                session.post(
                    f"{api_base}/shipyard/inbound/tickets/"
                    f"{ticket['ticket_id']}/receipts",
                    json={
                        **receipt_payload,
                        "idempotency_key": f"receipt-incomplete-{run_id}",
                        "pallets": pallets[:1],
                    },
                    timeout=20,
                ),
                422,
            )
            _json(
                session.post(
                    f"{api_base}/shipyard/inbound/tickets/"
                    f"{ticket['ticket_id']}/receipts",
                    json={
                        **receipt_payload,
                        "idempotency_key": f"receipt-duplicate-photo-{run_id}",
                        "pallets": [
                            pallets[0],
                            {
                                **pallets[1],
                                "photo_id": pallets[0]["photo_id"],
                            },
                        ],
                    },
                    timeout=20,
                ),
                404,
            )
            receipt_responses = _parallel_json_posts(
                api_base,
                session,
                f"/shipyard/inbound/tickets/{ticket['ticket_id']}/receipts",
                receipt_payload,
            )
            receipt_results = [_json(response, 200) for response in receipt_responses]
            receipt_ids = {
                result["receipt"]["receipt_id"] for result in receipt_results
            }
            if len(receipt_ids) != 1:
                raise AssertionError(receipt_ids)
            receipt = receipt_results[0]
            if receipt["ticket"]["on_hand_quantity"] != "4.000":
                raise AssertionError(receipt["ticket"])
            if receipt["ticket"]["status"] != "IN_STOCK":
                raise AssertionError(receipt["ticket"])
            _json(
                session.post(
                    f"{api_base}/shipyard/inbound/tickets/"
                    f"{ticket['ticket_id']}/receipts",
                    json={
                        **receipt_payload,
                        "idempotency_key": f"receipt-new-key-{run_id}",
                    },
                    timeout=20,
                ),
                409,
            )
            steps.append(
                "缺托、复用照片被拦截；50 请求/10 并发收货只落账一次，库存 4.000"
            )

            outbound_preview = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/formal-manifests/preview",
                    data={"warehouse_id": "1"},
                    files={
                        "file": (
                            "outbound-e2e.xlsx",
                            BytesIO(
                                outbound_manifest_bytes(
                                    ship_no=ship_no,
                                    manufacturer="E2E 测试配件厂",
                                    material_code=material_code,
                                    item_name=item_name,
                                    item_spec="S-E2E",
                                    demand_quantity=4,
                                )
                            ),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    timeout=30,
                ),
                201,
            )
            outbound_manifest_id = outbound_preview["manifest"]["manifest_id"]
            outbound_order = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/formal-manifests/"
                    f"{outbound_manifest_id}/confirm",
                    json={},
                    timeout=20,
                ),
                200,
            )
            task = outbound_order["tasks"][0]
            steps.append("导入并确认正式出库清单，生成整票任务")

            receiver_session = _create_role_session(
                session,
                api_base,
                run_id=run_id,
                role="RECEIVER",
                page_keys=["dashboard", "inbound"],
                warehouse_ids=[1],
            )
            outbound_session = _create_role_session(
                session,
                api_base,
                run_id=run_id,
                role="OUTBOUND",
                page_keys=["dashboard", "inventory", "shipyard-outbound"],
                warehouse_ids=[1],
            )
            viewer_session = _create_role_session(
                session,
                api_base,
                run_id=run_id,
                role="VIEWER",
                page_keys=[
                    "dashboard",
                    "inventory",
                    "inbound",
                    "shipyard-outbound",
                    "audit-log",
                ],
                warehouse_ids=[1],
            )
            foreign_receiver = _create_role_session(
                session,
                api_base,
                run_id=f"{run_id}R2",
                role="RECEIVER",
                page_keys=["dashboard", "inbound"],
                warehouse_ids=[2],
            )
            foreign_outbound = _create_role_session(
                session,
                api_base,
                run_id=f"{run_id}O2",
                role="OUTBOUND",
                page_keys=["dashboard", "inventory", "shipyard-outbound"],
                warehouse_ids=[2],
            )
            receiver_without_page = _create_role_session(
                session,
                api_base,
                run_id=f"{run_id}RN",
                role="RECEIVER",
                page_keys=["dashboard"],
                warehouse_ids=[1],
            )
            outbound_without_page = _create_role_session(
                session,
                api_base,
                run_id=f"{run_id}ON",
                role="OUTBOUND",
                page_keys=["dashboard"],
                warehouse_ids=[1],
            )

            _json(
                receiver_session.post(
                    f"{api_base}/shipyard/inbound/tickets/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("receiver-role-check.png"),
                    timeout=10,
                ),
                404,
            )
            _json(
                receiver_session.post(
                    f"{api_base}/shipyard/outbound/tasks/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("receiver-denied.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                outbound_session.post(
                    f"{api_base}/shipyard/inbound/tickets/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("outbound-denied.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                outbound_session.post(
                    f"{api_base}/shipyard/outbound/tasks/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("outbound-role-check.png"),
                    timeout=10,
                ),
                404,
            )
            _json(
                viewer_session.post(
                    f"{api_base}/shipyard/inbound/tickets/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("viewer-denied.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                foreign_receiver.post(
                    f"{api_base}/shipyard/inbound/tickets/{ticket['ticket_id']}"
                    "/label-recognition",
                    files=_label_files("foreign-receiver-denied.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                foreign_outbound.post(
                    f"{api_base}/shipyard/outbound/tasks/{task['task_id']}"
                    "/label-recognition",
                    files=_label_files("foreign-outbound-denied.png"),
                    timeout=10,
                ),
                403,
            )
            foreign_resource_checks = [
                (
                    foreign_receiver,
                    "GET",
                    "/shipyard/inbound/manifests?warehouse_id=1",
                ),
                (
                    foreign_receiver,
                    "GET",
                    f"/shipyard/inbound/manifests/{manifest_id}",
                ),
                (
                    foreign_receiver,
                    "GET",
                    f"/shipyard/inbound/manifests/{manifest_id}/file",
                ),
                (
                    foreign_receiver,
                    "GET",
                    f"/shipyard/photos/{receipt_photo_ids[0]}",
                ),
                (
                    foreign_outbound,
                    "GET",
                    "/shipyard/outbound/formal-manifests?warehouse_id=1",
                ),
                (
                    foreign_outbound,
                    "GET",
                    f"/shipyard/outbound/formal-manifests/{outbound_manifest_id}",
                ),
                (
                    foreign_outbound,
                    "GET",
                    f"/shipyard/outbound/formal-manifests/{outbound_manifest_id}/file",
                ),
                (
                    foreign_outbound,
                    "GET",
                    "/shipyard/outbound/orders?warehouse_id=1",
                ),
                (
                    foreign_outbound,
                    "GET",
                    (
                        f"/shipyard/outbound/orders/"
                        f"{outbound_order['order']['outbound_order_id']}"
                    ),
                ),
                (
                    foreign_outbound,
                    "GET",
                    "/shipyard/outbound/mobile/orders?warehouse_id=1",
                ),
                (
                    foreign_outbound,
                    "GET",
                    (
                        f"/shipyard/outbound/mobile/orders/"
                        f"{outbound_order['order']['outbound_order_id']}"
                    ),
                ),
            ]
            for foreign_session, method, path in foreign_resource_checks:
                _json(
                    foreign_session.request(
                        method,
                        f"{api_base}{path}",
                        timeout=10,
                    ),
                    403,
                )
            _json(
                receiver_without_page.post(
                    f"{api_base}/shipyard/inbound/tickets/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("receiver-no-page.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                outbound_without_page.post(
                    f"{api_base}/shipyard/outbound/tasks/{uuid.uuid4()}"
                    "/label-recognition",
                    files=_label_files("outbound-no-page.png"),
                    timeout=10,
                ),
                403,
            )
            _json(
                outbound_session.get(
                    f"{api_base}/shipyard/outbound/mobile/orders?warehouse_id=1",
                    timeout=10,
                ),
                200,
            )
            _json(
                viewer_session.get(
                    f"{api_base}/shipyard/outbound/orders?warehouse_id=1",
                    timeout=10,
                ),
                200,
            )
            steps.append("角色权限和跨仓库隔离通过")

            outbound_turnover_service.recognize_label_image = _raise_ocr_provider
            provider_failure = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/"
                    f"{task['task_id']}/label-recognition",
                    files=_label_files("outbound-ocr-timeout.png"),
                    timeout=10,
                ),
                502,
            )
            if (
                provider_failure["recognition"]["status"] != "FAILED"
                or provider_failure["photo"]["photo_id"]
                != provider_failure["recognition"]["photo_id"]
            ):
                raise AssertionError(provider_failure)
            outbound_turnover_service.recognize_label_image = lambda _content: (
                outbound_ocr.pop(0)
            )
            steps.append("OCR 失败仍保留原图和 FAILED 记录，且不能核销")

            wrong_label = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/"
                    f"{task['task_id']}/label-recognition",
                    files=_label_files("outbound-wrong-material.png"),
                    timeout=10,
                ),
                201,
            )
            wrong_fields = {
                item["field"]
                for item in wrong_label["mismatches"]
                if item["reason"] == "MISMATCH"
            }
            if (
                wrong_label["matched"]
                or not {"material_code", "item_name"} <= wrong_fields
            ):
                raise AssertionError(wrong_label)
            steps.append("错误物料编码/品名被OCR规则定位并拒绝核销")

            evidence = []
            first_recognition = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/"
                    f"{task['task_id']}/label-recognition",
                    files={
                        "file": (
                            "outbound-1.png",
                            BytesIO(PNG_BYTES),
                            "image/png",
                        )
                    },
                    timeout=10,
                ),
                201,
            )
            if not first_recognition["needs_suffix"]:
                raise AssertionError("E2E 场景应触发缺后缀恢复")
            evidence.append(
                {
                    "tag_suffix": suffixes[0],
                    "label_recognition_id": first_recognition["recognition"][
                        "recognition_id"
                    ],
                    "photo_id": first_recognition["photo"]["photo_id"],
                }
            )
            steps.append("第一托 OCR 漏读后缀，返回剩余托盘供现场补选")

            second_recognition = _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/"
                    f"{task['task_id']}/label-recognition",
                    files={
                        "file": (
                            "outbound-2.png",
                            BytesIO(PNG_BYTES),
                            "image/png",
                        )
                    },
                    timeout=10,
                ),
                201,
            )
            if not second_recognition["matched"]:
                raise AssertionError(second_recognition)
            evidence.append(
                {
                    "tag_suffix": suffixes[1],
                    "label_recognition_id": second_recognition["recognition"][
                        "recognition_id"
                    ],
                    "photo_id": second_recognition["photo"]["photo_id"],
                }
            )
            steps.append("第二托 OCR 一次匹配，无额外输入")

            outbound_key = f"outbound-e2e-{run_id}"
            outbound_payload = {
                "idempotency_key": outbound_key,
                "client_device_id": f"wx-e2e-{run_id}",
                "pallets": evidence,
            }
            _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/{task['task_id']}/confirm",
                    json={
                        **outbound_payload,
                        "idempotency_key": f"outbound-incomplete-{run_id}",
                        "pallets": evidence[:1],
                    },
                    timeout=20,
                ),
                422,
            )
            _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/{task['task_id']}/confirm",
                    json={
                        **outbound_payload,
                        "idempotency_key": f"outbound-duplicate-photo-{run_id}",
                        "pallets": [
                            evidence[0],
                            {
                                **evidence[1],
                                "photo_id": evidence[0]["photo_id"],
                            },
                        ],
                    },
                    timeout=20,
                ),
                404,
            )
            outbound_responses = _parallel_json_posts(
                api_base,
                session,
                f"/shipyard/outbound/tasks/{task['task_id']}/confirm",
                outbound_payload,
            )
            outbound_results = [_json(response, 200) for response in outbound_responses]
            confirmation_ids = {
                result["confirmation"]["confirmation_id"] for result in outbound_results
            }
            if len(confirmation_ids) != 1:
                raise AssertionError(confirmation_ids)
            confirmed = outbound_results[0]
            if confirmed["confirmation"]["balance_after"] != "0.000":
                raise AssertionError(confirmed["confirmation"])
            _json(
                session.post(
                    f"{api_base}/shipyard/outbound/tasks/{task['task_id']}/confirm",
                    json={
                        **outbound_payload,
                        "idempotency_key": f"outbound-new-key-{run_id}",
                    },
                    timeout=20,
                ),
                409,
            )
            steps.append("缺托和复用照片被拦截；50 请求/10 并发核销只扣库一次")

            loading_key = f"loading-e2e-{run_id}"
            loading_payload = {
                "idempotency_key": loading_key,
                "client_device_id": f"wx-e2e-{run_id}",
                "notes": "E2E 两托已装车",
            }
            loading_responses = _parallel_json_posts(
                api_base,
                session,
                f"/shipyard/outbound/orders/"
                f"{outbound_order['order']['outbound_order_id']}/loading",
                loading_payload,
            )
            loading_results = [_json(response, 200) for response in loading_responses]
            if any(result["order"]["status"] != "LOADED" for result in loading_results):
                raise AssertionError(loading_results)
            _json(
                session.post(
                    f"{api_base}/shipyard/outbound/orders/"
                    f"{outbound_order['order']['outbound_order_id']}/loading",
                    json={
                        **loading_payload,
                        "idempotency_key": f"loading-new-key-{run_id}",
                    },
                    timeout=20,
                ),
                409,
            )
            steps.append("50 请求/10 并发装车只写一次，整单进入 LOADED")

            refreshed_order = _json(
                session.get(
                    f"{api_base}/shipyard/outbound/orders/"
                    f"{outbound_order['order']['outbound_order_id']}",
                    timeout=10,
                ),
                200,
            )
            if refreshed_order["order"]["status"] != "LOADED":
                raise AssertionError(refreshed_order["order"])
            if refreshed_order["order"].get("loading", {}).get("notes") != (
                "E2E 两托已装车"
            ):
                raise AssertionError(refreshed_order["order"])
            steps.append("后台重新查询出库单，状态和装车备注同步")

            first_photo = session.get(
                f"{api_base}/shipyard/photos/{receipt_photo_ids[0]}",
                timeout=10,
            )
            if first_photo.status_code != 200 or first_photo.content != PNG_BYTES:
                raise AssertionError(f"照片证据读取失败: {first_photo.status_code}")
            steps.append("原图证据可重新读取且字节一致")

            evidence_summary = _database_evidence(
                ticket["ticket_id"],
                confirmed["confirmation"]["confirmation_id"],
            )
            expected_database = {
                "on_hand_quantity": "0.000",
                "ticket_status": "OUTBOUND",
                "confirmation_pallets": 2,
                "recognition_links": 2,
                "photo_links": 2,
                "outbound_ledger_count": 1,
                "outbound_delta": "-4.000",
            }
            if evidence_summary != expected_database:
                raise AssertionError(evidence_summary)

            invariants = _database_invariants()
            if not invariants["passed"]:
                raise AssertionError(invariants)
            steps.append("库存流水、照片/OCR 归属和单据状态不变量通过")

            return {
                "status": "passed",
                "api": api_base,
                "run_id": run_id,
                "ship_no": ship_no,
                "steps": steps,
                "order_status": refreshed_order["order"]["status"],
                "on_hand_quantity": "0.000",
                "confirmation_id": confirmed["confirmation"]["confirmation_id"],
                "database": evidence_summary,
                "invariants": invariants,
                "ocr": "deterministic stub (no public credential configured)",
            }
    finally:
        conftest._seed_database()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
