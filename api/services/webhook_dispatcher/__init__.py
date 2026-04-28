"""Webhook dispatcher daemon (v1.6.0 #173).

Standalone process: ``python -m services.webhook_dispatcher``.

Mirrors the v1.5.0 ``snapshot_keeper`` shape (single Python process,
heartbeat-file healthcheck, SIGTERM-aware loop). Per plan section 2.1
the dispatcher will fan out three concurrency primitives:

  1. A per-subscription delivery thread (D5) that reads pending rows
     from ``webhook_deliveries`` and POSTs them to the consumer.
  2. A wake thread (D3) listening on the ``integration_events_visible``
     pg_notify channel from migration 031 with a 2-second fallback poll.
  3. A Redis pubsub subscriber (D3 + D4) on the
     ``webhook_subscription_events`` channel for cross-worker
     invalidation per plan section 2.9.

D1 (this commit) lands ONLY the daemon scaffolding: env validation,
SIGTERM handling, heartbeat write, and the ``DISPATCHER_ENABLED``
kill switch. The real loop bodies are stubs filled in by D2 (signing),
D3 (wake), D5 (dispatch), and so on. Booting D1 produces a process
that writes a heartbeat every 5s, exits cleanly on SIGTERM, and does
no real dispatch work yet -- so an operator can stand up the
container and confirm the wiring is correct before any consumer
delivery surface goes live.

The kill switch (``DISPATCHER_ENABLED=false``) lets an operator stop
dispatch traffic without removing the container; the heartbeat keeps
the docker-compose healthcheck green so the container does not enter
a restart loop. The boot path logs CRITICAL when the kill switch is
active so a stale-config "why isn't this dispatching?" question
surfaces in ``docker compose logs`` immediately.
"""

import logging
import os
import signal
import sys
import time
from typing import Optional

from . import dispatch as dispatch_module
from . import env_validator
from . import wake as wake_module


LOGGER = logging.getLogger("webhook_dispatcher")


HEARTBEAT_INTERVAL_S = 5
HEARTBEAT_FILE_DEFAULT = "/tmp/webhook-dispatcher-heartbeat"


class WebhookDispatcher:
    """Daemon entry-point class. D1 only handles boot, env
    validation, kill switch, heartbeat, and clean shutdown. The
    real dispatch loop lands in D5; the wake threads in D3."""

    def __init__(
        self,
        heartbeat_file: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.heartbeat_file = heartbeat_file or os.environ.get(
            "DISPATCHER_HEARTBEAT_FILE", HEARTBEAT_FILE_DEFAULT
        )
        # ``enabled`` is read at run() time, not import time, so a
        # test that toggles the env var per-call sees the new value
        # (mirrors V-217 #156 lesson on module-level env reads).
        self._enabled_override = enabled
        self._shutdown = False
        self._last_heartbeat_monotonic = 0.0
        self._wake: Optional[wake_module.WakeOrchestrator] = None
        self._pool: Optional[dispatch_module.SubscriptionWorkerPool] = None

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        # Default true. Only a case-insensitive "false" disables;
        # ambiguous falsy-looking strings ("0", "no", "off", "") do
        # NOT disable so an accidental config from another project
        # cannot silently engage the kill switch. Operators may
        # write "False" / "FALSE" -- the lower() handles that
        # without admitting the looser values.
        return os.environ.get("DISPATCHER_ENABLED", "true").lower() != "false"

    def run(self):
        """Boot sequence: validate env, install signal handlers,
        announce state, start wake threads (if not in kill-switch
        mode), enter heartbeat loop. Returns when the loop exits
        (SIGTERM / SIGINT)."""
        env_validator.validate_or_die()
        self._install_signal_handlers()

        if not self.enabled:
            LOGGER.critical(
                "dispatcher kill-switch active: DISPATCHER_ENABLED=false; "
                "this container will write heartbeats but perform no dispatch. "
                "Set DISPATCHER_ENABLED=true (or unset) and restart to resume."
            )
        else:
            database_url = os.environ.get(
                "DISPATCHER_DATABASE_URL"
            ) or os.environ["DATABASE_URL"]
            self._wake = wake_module.WakeOrchestrator(
                database_url=database_url,
                redis_url=os.environ.get("REDIS_URL"),
                fallback_poll_ms=env_validator.int_var(
                    "DISPATCHER_FALLBACK_POLL_MS"
                ),
            )
            self._wake.start()
            self._pool = dispatch_module.SubscriptionWorkerPool(
                database_url=database_url,
                wake_queue=self._wake.queue,
                redis_url=os.environ.get("REDIS_URL"),
            )
            self._pool.start()
            LOGGER.info(
                "webhook-dispatcher started (heartbeat=%s); D5 dispatch loop "
                "running (per-subscription workers + fanout from wake queue).",
                self.heartbeat_file,
            )

        try:
            while not self._shutdown:
                self._write_heartbeat()
                # The wake orchestrator and the worker pool run
                # in their own threads; the main loop just keeps
                # the heartbeat file fresh and the daemon
                # responsive to SIGTERM. D6/D7 will adjust the
                # pool's per-subscription state from this loop
                # (auto-pause cleanup, retry-slot rescheduling
                # nudges); D5 leaves it minimal.
                time.sleep(1.0)
        finally:
            drain_s = float(env_validator.int_var(
                "DISPATCHER_SHUTDOWN_DRAIN_S"
            ))
            if self._pool is not None:
                LOGGER.info(
                    "webhook-dispatcher shutting down worker pool "
                    "(drain timeout %.1fs)",
                    drain_s,
                )
                self._pool.shutdown()
                self._pool.join(timeout_s=drain_s)
            if self._wake is not None:
                LOGGER.info("webhook-dispatcher shutting down wake threads")
                self._wake.shutdown()
                self._wake.join(timeout_s=drain_s)
            LOGGER.info("webhook-dispatcher exiting")

    def _install_signal_handlers(self):
        signal.signal(signal.SIGTERM, lambda *_a: self._request_shutdown("SIGTERM"))
        signal.signal(signal.SIGINT, lambda *_a: self._request_shutdown("SIGINT"))

    def _request_shutdown(self, reason: str):
        LOGGER.info("shutdown requested (%s)", reason)
        self._shutdown = True

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
        level=os.environ.get("DISPATCHER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dispatcher = WebhookDispatcher()
    dispatcher.run()


if __name__ == "__main__":
    main()
