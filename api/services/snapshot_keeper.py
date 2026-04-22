"""Snapshot keeper daemon for v1.5.0 bulk-snapshot paging (#132).

Standalone process: ``python -m services.snapshot_keeper``.

Responsibilities:

1. LISTEN for pending scans on ``snapshot_scans_pending``. A 1-second
   fallback poll catches NOTIFYs missed during a keeper restart
   (NOTIFY is not durable across a disconnect).
2. For each pending scan (up to ``pool_size=4`` concurrent): open a
   REPEATABLE READ transaction, capture
   ``snapshot_event_id = MAX(event_id) WHERE visible_at <= NOW() - 2s``,
   export a ``pg_snapshot_id`` via ``pg_export_snapshot()``, write
   both back to the row, and flip ``status='active'``.
3. Hold the exporting transaction idle so the API can import the
   same snapshot on short-lived connections via
   ``SET TRANSACTION SNAPSHOT '<id>'``.
4. Reap scans whose row transitioned to ``done``/``aborted`` (the
   API tier flips it) or exceeded the 5-minute idle timeout.
   ``COMMIT`` the held transaction and ``DELETE`` the row.
5. On boot: any row still in ``active`` state points at a
   pg_snapshot_id whose exporting transaction died with the previous
   keeper. Flip them to ``aborted`` so the API returns 410 on the
   next poll.
6. On SIGTERM: close every held transaction cleanly before exit.

Single-keeper architecture (plan R12). Horizontal scale is deferred.
"""

import logging
import os
import select
import signal
import sys
import time
from typing import Dict, Optional

import psycopg2


LOGGER = logging.getLogger("snapshot_keeper")


DEFAULT_POOL_SIZE = 4
DEFAULT_IDLE_TIMEOUT_S = 300  # 5 minutes (plan 4.1)
DEFAULT_POLL_INTERVAL_S = 1.0
HEARTBEAT_INTERVAL_S = 5
HEARTBEAT_FILE_DEFAULT = "/tmp/snapshot-keeper-heartbeat"


class SnapshotKeeper:
    def __init__(
        self,
        database_url: Optional[str] = None,
        pool_size: int = DEFAULT_POOL_SIZE,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        heartbeat_file: Optional[str] = None,
    ):
        self.database_url = database_url or os.environ["DATABASE_URL"]
        self.pool_size = pool_size
        self.idle_timeout_s = idle_timeout_s
        self.poll_interval_s = poll_interval_s
        self.heartbeat_file = heartbeat_file or os.environ.get(
            "SNAPSHOT_KEEPER_HEARTBEAT_FILE", HEARTBEAT_FILE_DEFAULT
        )
        self.active: Dict[str, dict] = {}
        self._shutdown = False
        self._listen_conn: Optional[psycopg2.extensions.connection] = None
        self._last_heartbeat_monotonic = 0.0

    # ── Public entry point ──────────────────────────────────────────

    def run(self):
        self._install_signal_handlers()
        self._open_listener()
        self._cleanup_orphans_on_boot()
        LOGGER.info(
            "snapshot-keeper started (pool_size=%d idle_timeout_s=%d)",
            self.pool_size,
            self.idle_timeout_s,
        )
        try:
            while not self._shutdown:
                self._write_heartbeat()
                self._promote_pending()
                self._reap_completed_or_timed_out()
                self._drain_notifications(self.poll_interval_s)
        finally:
            self._graceful_shutdown()

    # ── Signal handling ─────────────────────────────────────────────

    def _install_signal_handlers(self):
        signal.signal(signal.SIGTERM, lambda *_a: self._request_shutdown("SIGTERM"))
        signal.signal(signal.SIGINT, lambda *_a: self._request_shutdown("SIGINT"))

    def _request_shutdown(self, reason: str):
        LOGGER.info("shutdown requested (%s)", reason)
        self._shutdown = True

    # ── Connection helpers ──────────────────────────────────────────

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def _short_lived(self):
        """Return an autocommit connection for a one-off read/write.

        Yields the cursor + its owning connection so the caller can
        close both in a ``with`` block. Opening a connection per call
        is fine at keeper throughput (O(1)/s); pooling would add
        complexity without a measurable win.
        """
        class _Ctx:
            def __init__(inner):
                inner.conn = None
                inner.cur = None

            def __enter__(inner):
                inner.conn = self._connect()
                inner.conn.autocommit = True
                inner.cur = inner.conn.cursor()
                return inner.cur

            def __exit__(inner, *exc):
                try:
                    inner.cur.close()
                finally:
                    inner.conn.close()

        return _Ctx()

    # ── LISTEN / NOTIFY ─────────────────────────────────────────────

    def _open_listener(self):
        self._listen_conn = self._connect()
        self._listen_conn.autocommit = True
        cur = self._listen_conn.cursor()
        cur.execute("LISTEN snapshot_scans_pending")
        cur.close()

    def _drain_notifications(self, timeout_s: float):
        if self._listen_conn is None:
            time.sleep(timeout_s)
            return
        rlist, _, _ = select.select([self._listen_conn], [], [], timeout_s)
        if not rlist:
            return
        self._listen_conn.poll()
        # Payload is informational; the main loop polls the DB for
        # the authoritative set of pending rows every iteration.
        self._listen_conn.notifies.clear()

    # ── Boot orphan cleanup ─────────────────────────────────────────

    def _cleanup_orphans_on_boot(self):
        with self._short_lived() as cur:
            cur.execute(
                "UPDATE snapshot_scans SET status='aborted' "
                "WHERE status='active'"
            )
            # psycopg2 reports rowcount after the execute.
            if cur.rowcount:
                LOGGER.info(
                    "aborted %d orphan 'active' scan(s) on boot", cur.rowcount
                )

    # ── Promotion: pending -> active ────────────────────────────────

    def _promote_pending(self):
        free = self.pool_size - len(self.active)
        if free <= 0:
            return
        with self._short_lived() as cur:
            cur.execute(
                "SELECT scan_id FROM snapshot_scans "
                " WHERE status='pending' "
                " ORDER BY started_at "
                " LIMIT %s",
                (free,),
            )
            pending_ids = [str(r[0]) for r in cur.fetchall()]
        for scan_id in pending_ids:
            self._promote(scan_id)

    def _promote(self, scan_id: str):
        held = self._connect()
        held.autocommit = False
        cur = held.cursor()
        try:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            cur.execute(
                "SELECT COALESCE(MAX(event_id), 0) "
                "  FROM integration_events "
                " WHERE visible_at IS NOT NULL "
                "   AND visible_at <= NOW() - INTERVAL '2 seconds'"
            )
            snapshot_event_id = cur.fetchone()[0]
            cur.execute("SELECT pg_export_snapshot()")
            pg_snapshot_id = cur.fetchone()[0]

            # Race-safe promotion: the UPDATE only lands while status
            # is still 'pending'. If another process beat us to it,
            # abandon the held RR transaction.
            with self._short_lived() as scur:
                scur.execute(
                    "UPDATE snapshot_scans "
                    "   SET status='active', "
                    "       pg_snapshot_id=%s, "
                    "       snapshot_event_id=%s, "
                    "       last_accessed_at=NOW() "
                    " WHERE scan_id=%s AND status='pending' "
                    "RETURNING scan_id",
                    (pg_snapshot_id, snapshot_event_id, scan_id),
                )
                promoted = scur.fetchone()
            if promoted is None:
                LOGGER.info("scan %s already promoted or cancelled", scan_id)
                held.rollback()
                held.close()
                return
            self.active[scan_id] = {
                "conn": held,
                "promoted_at_monotonic": time.monotonic(),
                "pg_snapshot_id": pg_snapshot_id,
                "snapshot_event_id": snapshot_event_id,
            }
            LOGGER.info(
                "promoted scan %s pg_snapshot_id=%s snapshot_event_id=%d",
                scan_id,
                pg_snapshot_id,
                snapshot_event_id,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("failed to promote scan %s", scan_id)
            try:
                held.rollback()
            finally:
                held.close()

    # ── Reaping: active -> done / expired / aborted ─────────────────

    def _reap_completed_or_timed_out(self):
        if not self.active:
            return
        ids = list(self.active.keys())
        with self._short_lived() as cur:
            cur.execute(
                "SELECT scan_id, status, last_accessed_at "
                "  FROM snapshot_scans "
                " WHERE scan_id::text = ANY(%s)",
                (ids,),
            )
            rows = cur.fetchall()
        now_monotonic = time.monotonic()
        row_ids_seen = {str(r[0]) for r in rows}

        to_close = []
        for scan_id_raw, status, _last_accessed_at in rows:
            scan_id = str(scan_id_raw)
            if scan_id not in self.active:
                continue
            entry = self.active[scan_id]
            idle_s = now_monotonic - entry["promoted_at_monotonic"]
            if status in ("done", "aborted"):
                to_close.append((scan_id, status))
            elif idle_s > self.idle_timeout_s:
                to_close.append((scan_id, "expired"))

        # Rows we're tracking but that no longer appear in the table
        # (deleted by some other actor): close the held transaction
        # and drop the in-memory entry.
        for scan_id in ids:
            if scan_id not in row_ids_seen:
                to_close.append((scan_id, "deleted"))

        for scan_id, final_status in to_close:
            self._close(scan_id, final_status)

    def _close(self, scan_id: str, final_status: str):
        entry = self.active.pop(scan_id, None)
        if entry is None:
            return
        try:
            entry["conn"].commit()
        finally:
            try:
                entry["conn"].close()
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                pass
        # Row may already be gone if final_status='deleted'; the
        # DELETE is idempotent in that case.
        with self._short_lived() as cur:
            if final_status in ("expired",):
                cur.execute(
                    "UPDATE snapshot_scans SET status='expired' "
                    " WHERE scan_id::text=%s",
                    (scan_id,),
                )
            cur.execute(
                "DELETE FROM snapshot_scans WHERE scan_id::text=%s",
                (scan_id,),
            )
        LOGGER.info("closed scan %s (%s)", scan_id, final_status)

    # ── Shutdown + heartbeat ────────────────────────────────────────

    def _graceful_shutdown(self):
        LOGGER.info(
            "graceful shutdown, closing %d held scan(s)", len(self.active)
        )
        for scan_id in list(self.active.keys()):
            self._close(scan_id, "aborted")
        if self._listen_conn is not None:
            try:
                self._listen_conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _write_heartbeat(self):
        now = time.monotonic()
        if (now - self._last_heartbeat_monotonic) < HEARTBEAT_INTERVAL_S:
            return
        try:
            with open(self.heartbeat_file, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            LOGGER.warning("failed to write heartbeat to %s", self.heartbeat_file)
        self._last_heartbeat_monotonic = now


def main():
    logging.basicConfig(
        level=os.environ.get("SNAPSHOT_KEEPER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    keeper = SnapshotKeeper()
    keeper.run()


if __name__ == "__main__":
    main()
