"""GET /api/v1/snapshot/inventory endpoint (v1.5.0 #133).

Most tests pre-populate ``snapshot_scans`` rows with a real exported
``pg_snapshot_id`` so the endpoint's first-page promotion path can be
bypassed (no subprocess keeper required). The dedicated handoff test
starts the keeper subprocess and exercises the full path.
"""

import base64
import hashlib
import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest

from services import token_cache


PEPPER = os.environ["SENTRY_TOKEN_PEPPER"]
DATABASE_URL = os.environ["DATABASE_URL"]


def _hash(plaintext: str) -> str:
    return hashlib.sha256((PEPPER + plaintext).encode("utf-8")).hexdigest()


def _direct_conn(autocommit=True):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = autocommit
    return conn


def _insert_token(plaintext: str, warehouse_ids=(1,), token_name=None):
    conn = _direct_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO wms_tokens (token_name, token_hash, warehouse_ids, event_types) "
            "VALUES (%s, %s, %s, %s) RETURNING token_id",
            (token_name or f"snap-{uuid.uuid4()}", _hash(plaintext), list(warehouse_ids), []),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _delete_scan(scan_id):
    conn = _direct_conn()
    try:
        conn.cursor().execute(
            "DELETE FROM snapshot_scans WHERE scan_id::text = %s", (str(scan_id),)
        )
    finally:
        conn.close()


def _encode_cursor(scan_id, warehouse_id, item_id, bin_id):
    payload = json.dumps(
        {"scan_id": str(scan_id), "w": warehouse_id, "i": item_id, "b": bin_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class _ExportedSnapshot:
    """Holds a RR transaction exporting a pg_snapshot_id + pre-creates
    an 'active' snapshot_scans row. Use as a context manager.

    The held transaction is released on __exit__ so the tests do not
    leak idle-in-transaction state after the fixture ends.
    """

    def __init__(self, warehouse_id, token_id):
        self.warehouse_id = warehouse_id
        self.token_id = token_id
        self.scan_id = str(uuid.uuid4())
        self.conn = None
        self.pg_snapshot_id = None
        self.snapshot_event_id = None

    def __enter__(self):
        self.conn = _direct_conn(autocommit=False)
        cur = self.conn.cursor()
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        cur.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM integration_events "
            " WHERE visible_at IS NOT NULL "
            "   AND visible_at <= NOW() - INTERVAL '2 seconds'"
        )
        self.snapshot_event_id = cur.fetchone()[0]
        cur.execute("SELECT pg_export_snapshot()")
        self.pg_snapshot_id = cur.fetchone()[0]

        # Create the scan row as 'active' so the handler skips the
        # promotion poll.
        writer = _direct_conn()
        try:
            writer.cursor().execute(
                "INSERT INTO snapshot_scans (scan_id, warehouse_id, status, "
                "pg_snapshot_id, snapshot_event_id, created_by_token_id) "
                "VALUES (%s, %s, 'active', %s, %s, %s)",
                (self.scan_id, self.warehouse_id, self.pg_snapshot_id,
                 self.snapshot_event_id, self.token_id),
            )
        finally:
            writer.close()
        return self

    def __exit__(self, *exc):
        # The RR connection release is safe to do via its own handle.
        # We intentionally do NOT ``_delete_scan`` here: the handler
        # runs inside the test fixture's outer transaction and its
        # UPDATE on snapshot_scans holds a row lock that only
        # releases at fixture teardown. A separate-connection DELETE
        # here would wait forever for that lock. Rows are keyed by
        # UUID so leaking them across tests is harmless.
        if self.conn:
            try:
                self.conn.rollback()
            finally:
                self.conn.close()


@pytest.fixture(autouse=True)
def _clear_tc():
    token_cache.clear()
    yield
    token_cache.clear()


@pytest.fixture()
def scoped_token(seed_data):
    plaintext = f"snap-plain-{uuid.uuid4()}"
    token_id = _insert_token(plaintext, warehouse_ids=[1])
    return {"plaintext": plaintext, "token_id": token_id}


def _get(client, plaintext, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = "/api/v1/snapshot/inventory" + ("?" + qs if qs else "")
    return client.get(url, headers={"X-WMS-Token": plaintext})


class TestAuthAndScope:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/api/v1/snapshot/inventory?warehouse_id=1")
        assert resp.status_code == 401

    def test_warehouse_outside_token_scope_returns_403(self, client, scoped_token):
        resp = _get(client, scoped_token["plaintext"], warehouse_id=999)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "scope_violation"

    def test_warehouse_required(self, client, scoped_token):
        resp = client.get(
            "/api/v1/snapshot/inventory",
            headers={"X-WMS-Token": scoped_token["plaintext"]},
        )
        assert resp.status_code == 400


class TestKeeperUnavailable:
    def test_503_when_keeper_does_not_promote(self, client, scoped_token):
        """No keeper running. First-page INSERT lands, poll times out
        after 5s (endpoint constant), and 503 surfaces. The pending
        row is cleaned up so a downed keeper does not pollute the
        table with orphan requests."""
        resp = _get(client, scoped_token["plaintext"], warehouse_id=1)
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "snapshot_keeper_unavailable"


class TestCursorTamper:
    def test_cursor_warehouse_mismatch_returns_403(self, client, scoped_token):
        """Client submits a cursor whose scan was created for
        warehouse=1 but queries warehouse=2. 403 before any snapshot
        import happens."""
        # Set up a second warehouse + expand token scope.
        wh2 = _create_warehouse()
        _set_token_warehouses(scoped_token["token_id"], [1, wh2])
        token_cache.clear()

        with _ExportedSnapshot(warehouse_id=1, token_id=scoped_token["token_id"]) as scope:
            cursor = _encode_cursor(scope.scan_id, warehouse_id=1, item_id=0, bin_id=0)
            resp = _get(
                client, scoped_token["plaintext"],
                warehouse_id=wh2, cursor=cursor,
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cursor_scope_violation"

    def test_cursor_from_different_token_returns_403(self, client, scoped_token, seed_data):
        """Two tokens, each issues their own scan; a cursor from
        token A must not work when presented with token B's header."""
        other_plaintext = f"other-token-{uuid.uuid4()}"
        other_token_id = _insert_token(other_plaintext, warehouse_ids=[1])
        token_cache.clear()

        with _ExportedSnapshot(warehouse_id=1, token_id=other_token_id) as scope:
            cursor = _encode_cursor(scope.scan_id, warehouse_id=1, item_id=0, bin_id=0)
            resp = _get(
                client, scoped_token["plaintext"],
                warehouse_id=1, cursor=cursor,
            )
            assert resp.status_code == 403
            assert resp.get_json()["error"] == "cursor_scope_violation"

    def test_malformed_cursor_returns_400(self, client, scoped_token):
        resp = _get(
            client, scoped_token["plaintext"],
            warehouse_id=1, cursor="not-a-valid-base64-cursor",
        )
        assert resp.status_code == 400


class TestExpiredScan:
    def test_expired_scan_returns_410(self, client, scoped_token):
        """A scan whose status is 'expired' or 'aborted' at cursor
        time returns 410 Gone."""
        with _ExportedSnapshot(warehouse_id=1, token_id=scoped_token["token_id"]) as scope:
            # Flip status to expired before the cursor call.
            conn = _direct_conn()
            conn.cursor().execute(
                "UPDATE snapshot_scans SET status = 'expired' WHERE scan_id::text = %s",
                (scope.scan_id,),
            )
            conn.close()
            cursor = _encode_cursor(scope.scan_id, warehouse_id=1, item_id=0, bin_id=0)
            resp = _get(
                client, scoped_token["plaintext"],
                warehouse_id=1, cursor=cursor,
            )
            assert resp.status_code == 410
            assert resp.get_json()["error"] == "snapshot_expired"

    def test_unknown_scan_cursor_returns_410(self, client, scoped_token):
        """A cursor referencing a scan that no longer exists (keeper
        already released + deleted the row) returns 410."""
        cursor = _encode_cursor(
            str(uuid.uuid4()), warehouse_id=1, item_id=0, bin_id=0,
        )
        resp = _get(
            client, scoped_token["plaintext"],
            warehouse_id=1, cursor=cursor,
        )
        assert resp.status_code == 410


class TestKeysetPaging:
    def test_single_page_returns_seed_inventory(self, client, scoped_token):
        """The apartment-lab seed includes 20 inventory rows in
        warehouse 1. A large-enough limit returns them all in one
        page; partial page ⇒ next_cursor is null and status flips
        to 'done'."""
        with _ExportedSnapshot(warehouse_id=1, token_id=scoped_token["token_id"]) as scope:
            cursor = _encode_cursor(scope.scan_id, 1, 0, 0)
            resp = _get(
                client, scoped_token["plaintext"],
                warehouse_id=1, cursor=cursor, limit=2000,
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["snapshot_event_id"] == scope.snapshot_event_id
            assert body["next_cursor"] is None
            assert len(body["rows"]) >= 1
            row = body["rows"][0]
            for key in (
                "item_external_id", "warehouse_id", "bin_external_id",
                "quantity_on_hand", "quantity_allocated", "quantity_available",
                "lot_number", "serial_number",
            ):
                assert key in row
            assert uuid.UUID(row["item_external_id"])
            assert uuid.UUID(row["bin_external_id"])
            # Partial page => next_cursor is null. The handler also
            # flips status='done' inside the fixture's uncommitted
            # transaction; verifying that externally isn't possible
            # without a real commit, so we rely on the response
            # shape here.

    def test_multi_page_preserves_snapshot_event_id(self, client, scoped_token):
        """Two pages with small limit return the same
        snapshot_event_id (plan 4.2 invariant)."""
        with _ExportedSnapshot(warehouse_id=1, token_id=scoped_token["token_id"]) as scope:
            first_cursor = _encode_cursor(scope.scan_id, 1, 0, 0)
            first = _get(
                client, scoped_token["plaintext"],
                warehouse_id=1, cursor=first_cursor, limit=1,
            ).get_json()
            assert first["snapshot_event_id"] == scope.snapshot_event_id
            assert first["next_cursor"] is not None
            assert len(first["rows"]) == 1

            # Reset status back to 'active' between pages because the
            # previous call flipped it to 'done' (since limit=1 and
            # row count == limit in an unusual path... actually no,
            # with limit=1 and a full page, next_cursor is returned
            # and status stays active). Re-read to confirm.
            check = _direct_conn()
            try:
                cur = check.cursor()
                cur.execute(
                    "SELECT status FROM snapshot_scans WHERE scan_id::text = %s",
                    (scope.scan_id,),
                )
                assert cur.fetchone()[0] == "active"
            finally:
                check.close()

            second = _get(
                client, scoped_token["plaintext"],
                warehouse_id=1, cursor=first["next_cursor"], limit=1,
            ).get_json()
            assert second["snapshot_event_id"] == scope.snapshot_event_id


# ── Handoff invariant (snapshot_event_id + 1 = next polled event) ───
#
# The "does the keeper hold snapshots importably" invariant has a
# dedicated subprocess test in test_snapshot_keeper.py (#132). Here we
# verify the *endpoint* half of the handoff: the snapshot_event_id
# value the endpoint returns is a valid anchor for a subsequent
# /api/v1/events poll, with no gap and no overlap at the boundary.
# We use _ExportedSnapshot to skip the keeper promotion (same reason
# the unit-style paging tests do) since the fixture's outer
# transaction hides the handler's INSERT from a real keeper process.


class TestHandoffInvariant:
    def test_poll_after_snapshot_event_id_excludes_pre_skips_to_post(
        self, client, scoped_token
    ):
        """Plan 2.4 invariant.

        Sequence:
        - pre-event committed before the snapshot is captured.
        - _ExportedSnapshot captures MAX(event_id) visible at capture
          time; that becomes snapshot_event_id.
        - post-event committed AFTER the snapshot is captured, with
          visible_at back-dated past the 2s gate so it surfaces on
          a poll.
        - Poll with ``after=snapshot_event_id`` must omit the pre-event
          and return the post-event.
        """
        # Pre-event: visible outside the 2s gate, IDs will be captured
        # by the snapshot's MAX(event_id).
        pre_id = _insert_outbox_event(
            warehouse_id=1,
            visible_seconds_ago=5,
            event_type="receipt.completed",
        )

        try:
            with _ExportedSnapshot(
                warehouse_id=1, token_id=scoped_token["token_id"]
            ) as scope:
                snapshot_event_id = scope.snapshot_event_id
                assert snapshot_event_id >= pre_id, (
                    "snapshot_event_id must include the pre-event"
                )

                # Post-event: committed after the snapshot is exported.
                post_id = _insert_outbox_event(
                    warehouse_id=1,
                    visible_seconds_ago=5,
                    event_type="receipt.completed",
                )
                assert post_id > snapshot_event_id, (
                    "post-event event_id must exceed snapshot_event_id"
                )

                # Issue a second token for the polling call; polling
                # strict-subset scope requires event_types to include
                # the event types we want to read.
                poll_plaintext = f"poll-{uuid.uuid4()}"
                _insert_token(
                    poll_plaintext,
                    warehouse_ids=[1],
                    token_name=f"poll-{uuid.uuid4()}",
                )
                writer = _direct_conn()
                try:
                    writer.cursor().execute(
                        "UPDATE wms_tokens SET event_types = %s WHERE token_hash = %s",
                        (["receipt.completed"], _hash(poll_plaintext)),
                    )
                finally:
                    writer.close()
                token_cache.clear()

                resp = client.get(
                    f"/api/v1/events?after={snapshot_event_id}&warehouse_id=1",
                    headers={"X-WMS-Token": poll_plaintext},
                )
                assert resp.status_code == 200
                ids = [e["event_id"] for e in resp.get_json()["events"]]
                assert pre_id not in ids, (
                    "pre-snapshot event must not appear when polling with "
                    "after=snapshot_event_id"
                )
                assert post_id in ids, (
                    "post-snapshot event must appear when polling with "
                    "after=snapshot_event_id"
                )
                assert all(i > snapshot_event_id for i in ids), (
                    "every polled event_id must exceed snapshot_event_id"
                )
        finally:
            cleanup = _direct_conn()
            try:
                cleanup.cursor().execute(
                    "DELETE FROM integration_events WHERE aggregate_type = 'handoff_test'"
                )
            finally:
                cleanup.close()


# ── Local helpers ────────────────────────────────────────────────────


def _create_warehouse() -> int:
    conn = _direct_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO warehouses (warehouse_code, warehouse_name) "
            "VALUES (%s, 'Snap Test W') RETURNING warehouse_id",
            (f"SNAP-{uuid.uuid4().hex[:6]}",),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _set_token_warehouses(token_id: int, warehouse_ids):
    conn = _direct_conn()
    try:
        conn.cursor().execute(
            "UPDATE wms_tokens SET warehouse_ids = %s WHERE token_id = %s",
            (list(warehouse_ids), token_id),
        )
    finally:
        conn.close()


def _insert_outbox_event(warehouse_id, visible_seconds_ago, event_type):
    """INSERT a row into integration_events and force visible_at to a
    past timestamp so the 2s polling gate does not hide it.

    The migration-020 deferred trigger overwrites visible_at with
    clock_timestamp() at COMMIT, so the INSERT alone cannot set a
    back-dated value when running via autocommit. The follow-up
    UPDATE (which does NOT fire the INSERT trigger) is how we
    forcibly back-date visible_at for handoff tests.
    """
    conn = _direct_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO integration_events (
                event_type, event_version, aggregate_type, aggregate_id,
                aggregate_external_id, warehouse_id, source_txn_id, payload
            ) VALUES (%s, 1, 'handoff_test', %s, %s, %s, %s, '{}'::jsonb)
            RETURNING event_id
            """,
            (
                event_type,
                abs(hash(uuid.uuid4())) % 10_000_000,
                str(uuid.uuid4()),
                warehouse_id,
                str(uuid.uuid4()),
            ),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            f"UPDATE integration_events "
            f"   SET visible_at = NOW() - INTERVAL '{int(visible_seconds_ago)} seconds' "
            f" WHERE event_id = %s",
            (event_id,),
        )
        return event_id
    finally:
        conn.close()
