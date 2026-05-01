"""Healthcheck CLI for the docker-compose ``healthcheck`` directive.

Invoked as ``python -m services.webhook_dispatcher.healthcheck``.
Exits 0 when the heartbeat file is newer than the staleness
threshold, non-zero otherwise. Mirrors the snapshot-keeper
healthcheck pattern (which uses a CMD-SHELL ``find`` invocation
inline in docker-compose; this module exists per plan §2.1 to
keep the healthcheck logic version-controlled with the daemon).

Staleness threshold defaults to 30s, twice the 5s
``HEARTBEAT_INTERVAL_S`` baked into the daemon. A miss means the
loop has hung for at least one full interval; longer than 30s
means the daemon is wedged badly enough that docker-compose
should restart the container.
"""

import os
import sys
import time
from typing import Optional

from . import HEARTBEAT_FILE_DEFAULT


STALENESS_THRESHOLD_S = 30


def is_healthy(
    heartbeat_file: Optional[str] = None,
    threshold_s: int = STALENESS_THRESHOLD_S,
    now_fn=time.time,
) -> bool:
    """Return True when the heartbeat file mtime is within
    ``threshold_s`` of ``now_fn()``. ``now_fn`` is injected so a
    test can assert behavior at controlled timestamps without
    waiting wall-clock seconds."""
    path = heartbeat_file or os.environ.get(
        "DISPATCHER_HEARTBEAT_FILE", HEARTBEAT_FILE_DEFAULT
    )
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return (now_fn() - mtime) <= threshold_s


def main() -> int:
    return 0 if is_healthy() else 1


if __name__ == "__main__":
    sys.exit(main())
