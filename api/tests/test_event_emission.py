"""Per-handler emission integration tests (v1.5.0 outbox).

One class per emit site (receipt.completed, adjustment.applied,
transfer.completed, pick.confirmed, pack.confirmed, ship.confirmed,
cycle_count.adjusted). Each drives the real handler via the existing
cookie-auth + SQLAlchemy-savepoint fixture and asserts that the
``integration_events`` row lands with the right envelope shape.

``visible_at`` commit-ordering and concurrency coverage live in
``test_events_migration.py`` (schema-level) and ``test_event_fifo.py``
(to be added in #120). The fixture used here wraps each test in a
rollback'd outer transaction, so the deferred ``visible_at`` trigger
does not fire inside the test window. The tests instead assert the
row lands with ``visible_at IS NULL`` pre-commit; the trigger's
post-commit behaviour is already proven in ``test_events_migration``.
"""

import json
import uuid

from db_test_context import get_raw_connection


def _query_event_rows(source_txn_id: str):
    """Return every integration_events row emitted during this test's
    wrapping transaction with the given source_txn_id.

    Uses the raw test connection (same savepoint-owning connection the
    handler ran against) so visibility inside the outer transaction
    matches what the handler wrote.
    """
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_id, event_type, event_version, aggregate_type,
               aggregate_id, aggregate_external_id::text, warehouse_id,
               source_txn_id::text, visible_at, payload
          FROM integration_events
         WHERE source_txn_id = %s
         ORDER BY event_id
        """,
        (source_txn_id,),
    )
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    for row in rows:
        # psycopg2 decodes JSONB to dict already; fall back for str case.
        if isinstance(row["payload"], str):
            row["payload"] = json.loads(row["payload"])
    return rows


class TestReceiptCompletedEmission:
    def test_single_item_receive_emits_one_event(self, client, auth_headers, seed_data):
        # Stable X-Request-ID so the test can filter integration_events by
        # source_txn_id rather than scanning every row in the table.
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {
                        "item_id": 1,
                        "quantity": 3,
                        "bin_id": seed_data["staging_bin_id"],
                        "lot_number": "LOT-TEST-112",
                        "serial_number": None,
                    }
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200, resp.get_json()

        rows = _query_event_rows(request_id)
        assert len(rows) == 1, f"expected exactly one receipt.completed row, got {len(rows)}"
        row = rows[0]
        assert row["event_type"] == "receipt.completed"
        assert row["event_version"] == 1
        assert row["aggregate_type"] == "item_receipt"
        assert row["warehouse_id"] == seed_data["warehouse_id"]
        # Aggregate external_id is a UUID string, not the internal receipt_id.
        assert uuid.UUID(row["aggregate_external_id"])
        # Deferred trigger does not fire inside the test fixture's outer
        # transaction, so visible_at stays NULL here. The trigger's
        # COMMIT-time behaviour is proven in test_events_migration.py.
        assert row["visible_at"] is None

    def test_receipt_payload_matches_schema(self, client, auth_headers, seed_data):
        request_id = str(uuid.uuid4())
        client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {
                        "item_id": 1,
                        "quantity": 5,
                        "bin_id": seed_data["staging_bin_id"],
                        "lot_number": "LOT-TEST-112-B",
                        "serial_number": "SN-42",
                    }
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        payload = rows[0]["payload"]

        # Field-by-field checks against docs/events/receipt.completed/1.json.
        assert uuid.UUID(payload["receipt_external_id"])
        assert uuid.UUID(payload["po_external_id"])
        assert uuid.UUID(payload["completed_by_user_external_id"])
        assert payload["completed_at"].endswith("Z") or "+" in payload["completed_at"]

        lines = payload["lines"]
        assert len(lines) == 1  # v1.5.0 ships single-line per receipt row
        line = lines[0]
        assert uuid.UUID(line["item_external_id"])
        assert line["quantity_received"] == 5
        assert line["lot_number"] == "LOT-TEST-112-B"
        assert line["serial_number"] == "SN-42"

    def test_multi_item_receive_emits_one_event_per_receipt_row(
        self, client, auth_headers, seed_data
    ):
        """Sentry creates one item_receipts row per item in the call, so
        the emit site fires one receipt.completed per row. Confirmed by
        counting events after a 3-item receive."""
        request_id = str(uuid.uuid4())
        resp = client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 2, "bin_id": seed_data["staging_bin_id"]},
                    {"item_id": 2, "quantity": 3, "bin_id": seed_data["staging_bin_id"]},
                    {"item_id": 3, "quantity": 4, "bin_id": seed_data["staging_bin_id"]},
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        assert resp.status_code == 200
        rows = _query_event_rows(request_id)
        assert len(rows) == 3
        quantities = sorted(r["payload"]["lines"][0]["quantity_received"] for r in rows)
        assert quantities == [2, 3, 4]

    def test_source_txn_id_prefers_x_request_id_header(
        self, client, auth_headers, seed_data
    ):
        """Plan 1.5: X-Request-ID wins over a generated id when it parses
        as a UUID. The handler's emit call must therefore carry the
        header UUID as source_txn_id on the resulting row."""
        request_id = "7c9f4a2a-6fac-4e3b-8c0a-8d2f9d7ab012"
        client.post(
            "/api/receiving/receive",
            json={
                "po_id": 1,
                "items": [
                    {"item_id": 1, "quantity": 1, "bin_id": seed_data["staging_bin_id"]},
                ],
            },
            headers={**auth_headers, "X-Request-ID": request_id},
        )
        rows = _query_event_rows(request_id)
        assert len(rows) == 1
        assert rows[0]["source_txn_id"] == request_id
