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

# V-024: run cleanup tasks on a beat schedule. Celery beat must be running
# for these to fire -- add a celery-beat service to docker-compose alongside
# the worker, or run `celery -A jobs beat` in the same container as the
# worker when a single-process deployment is fine.
celery_app.conf.beat_schedule = {
    "cleanup-login-attempts-every-15-min": {
        "task": "jobs.cleanup_tasks.cleanup_login_attempts",
        "schedule": 15 * 60.0,  # seconds
    },
    "cleanup-webhook-deliveries-every-6-hours": {
        "task": "jobs.cleanup_tasks.cleanup_webhook_deliveries",
        "schedule": 6 * 3600.0,
    },
    "cleanup-expired-webhook-secrets-every-hour": {
        "task": "jobs.cleanup_tasks.cleanup_expired_webhook_secrets",
        "schedule": 3600.0,
    },
    # v1.7.0 R6: NULL source_payload on inbound rows older than the
    # configured retention window (default 90 days, hard floor 7).
    # 24h cadence is enough -- retention is measured in days, not
    # hours; running more often just churns indexes.
    "cleanup-inbound-source-payload-daily": {
        "task": "jobs.cleanup_tasks.cleanup_inbound_source_payload",
        "schedule": 24 * 3600.0,
    },
}

# Auto-discover task modules in the jobs package
celery_app.autodiscover_tasks(["jobs"], related_name="sync_tasks")
celery_app.autodiscover_tasks(["jobs"], related_name="cleanup_tasks")

# Import connector modules so they auto-register in worker processes
import connectors.example  # noqa: E402, F401
