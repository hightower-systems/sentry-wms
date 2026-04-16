"""Celery application for background job processing.

Sentry WMS uses Celery to run connector sync operations asynchronously.
The Flask API thread never blocks on external ERP calls -- warehouse
scanners stay responsive while syncs run in the background.

Broker and result backend default to Redis. Configure via environment:
    CELERY_BROKER_URL      (default: redis://redis:6379/0)
    CELERY_RESULT_BACKEND  (default: redis://redis:6379/0)
"""

import os

from celery import Celery

broker_url = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

celery_app = Celery(
    "sentry_wms",
    broker=broker_url,
    backend=result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Auto-discover task modules in the jobs package
celery_app.autodiscover_tasks(["jobs"], related_name="sync_tasks")

# Import connector modules so they auto-register in worker processes
import connectors.example  # noqa: E402, F401
