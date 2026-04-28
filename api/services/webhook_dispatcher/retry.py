"""Retry schedule + terminal transitions + ceiling auto-pause.

Filled in by D6 + D7. Plan §2.4 hard-codes the schedule:
``RETRY_SCHEDULE_SECONDS = [1, 4, 15, 60, 5*60, 30*60, 2*3600, 12*3600]``.
Eight attempts, DLQ on the eighth. The ceiling auto-pause flips
the subscription to ``status='paused'`` with ``pause_reason='dlq_ceiling'``
or ``pause_reason='pending_ceiling'`` and publishes the change on
``webhook_subscription_events`` so other workers tear down their
session for the affected subscription.
"""
