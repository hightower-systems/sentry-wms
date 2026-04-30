# Webhook pending-ceiling auto-pause runbook

Audience: operators responding to a subscription that auto-paused with `pause_reason='pending_ceiling'`.

Scope: how the pending ceiling differs from the DLQ ceiling, the decision tree for "subscription auto-paused due to pending ceiling, head event is unprocessable," the unblock procedure, and how to tune the per-subscription ceiling vs the deployment hard cap.

## What the pending ceiling is

Every webhook subscription has a `pending_ceiling` (default 10,000). When the count of `webhook_deliveries` rows in `pending` or `in_flight` for the subscription reaches the ceiling, the dispatcher flips the subscription to `paused` with `pause_reason='pending_ceiling'` atomically with the ceiling-th INSERT.

The pending ceiling is distinct from the DLQ ceiling. DLQ ceiling fires when the count of *terminal* `dlq` rows reaches 1,000 (default); pending ceiling fires when the count of *in-progress* rows reaches 10,000. Both auto-pause; the difference is what the ceiling is counting and what the operator's job is afterwards.

## When the pending ceiling fires

In a healthy steady state, the pending count for a subscription stays under ~50 (one in-flight POST + a thin queue of pending rows the dispatcher has not yet picked up). The pending ceiling at 10,000 represents:

- A consumer that is up but extremely slow (say, 1 event/sec sustained while emit rate is 5 events/sec) will reach the ceiling in ~33 minutes once the queue starts growing.
- A consumer that has gone down completely will not reach the pending ceiling on its own; it will reach the DLQ ceiling first because every event eventually exhausts its 8 retries and lands in DLQ. The pending ceiling fires when the *retry* schedule itself produces enough pending rows to pile up: a consumer that 5xxs every request inserts a fresh `pending` row at each of attempts 1-8, so a sustained burst of failures produces ~8 pending rows per event.
- A consumer that succeeds on attempt 1 most of the time but the dispatcher cannot keep up (low `DISPATCHER_MAX_CONCURRENT_POSTS`, slow DB, contention with another service) will see pending grow without DLQ growth.

The pending ceiling is therefore a "the dispatcher is falling behind for some reason" signal. The DLQ ceiling is a "the consumer cannot accept these events" signal. Different incidents.

## The hard case: head event is unprocessable

By design, the dispatcher processes events per subscription in cursor order. If the head event of the queue is unprocessable (a 4xx the consumer will never accept, a malformed payload from a bug in `emit_event`, a poison-pill event the consumer's verifier hates), the dispatcher exhausts its 8 retries on it, lands it in DLQ, advances the cursor, and moves on.

The pending ceiling can fire BEFORE that DLQ landing if the emit rate is high enough that fresh events accumulate while the head retries. In that case the operator opens the DLQ viewer and sees... nothing. The DLQ is empty. Every pending row is on the same head event.

That is the "head event is unprocessable" scenario this runbook is named for. The decision tree:

```
Subscription auto-paused with pause_reason='pending_ceiling'.
DLQ is empty or near-empty.

Step 1: Identify the head event.
  └── SELECT event_id, MIN(scheduled_at) FROM webhook_deliveries
        WHERE subscription_id = ?
          AND status IN ('pending', 'in_flight')
        GROUP BY event_id
        ORDER BY MIN(scheduled_at) ASC
        LIMIT 1;

Step 2: Inspect why the head event keeps failing.
  ├── Pull the source from integration_events; confirm the payload is
  │   the shape the consumer expects.
  ├── Read the in-progress row's error_kind / error_detail (catalog
  │   short_message). The pattern across the 8 attempts tells the
  │   shape of the failure.
  ├── If error_kind is 'tls' or 'connection': the consumer is down
  │   or unreachable. This is the wrong runbook; pause stays, wait
  │   for consumer recovery, then resume.
  └── If error_kind is '4xx' or '5xx': the consumer is up but
      rejecting this specific event.

Step 3: Choose one of three options:
  ├── Drop the head event.
  │     Manually flip the head's pending rows to 'dlq' so the
  │     cursor advances on resume. Use sparingly; this is the
  │     operator's "I have decided this event is not recoverable"
  │     button. Document in audit_log via a structured comment if
  │     your deployment uses one.
  │
  ├── Fix the head event.
  │     If the issue is in the emitted payload (a bug in event
  │     production), apply a code fix, then issue a replay-one
  │     against the head event. Be aware that simply replaying
  │     will hit the same failure mode if the underlying event
  │     row is unchanged; a code fix alone does not retroactively
  │     repair the integration_events row. The fix is usually
  │     either consumer-side (so a fresh re-dispatch works) or a
  │     manual UPDATE on the integration_events row by a
  │     warehouse-engineer who knows what shape the consumer
  │     wants.
  │
  └── Replay-batch the rest of the queue.
        This option only makes sense AFTER you have either dropped
        or fixed the head. With the head out of the way, the cursor
        advances naturally as the dispatcher works through the
        backlog. Replay-batch is for re-dispatching the DLQ rows
        that may have piled up alongside, NOT for the pending
        backlog (which the dispatcher will handle on resume).

Step 4: Resume the subscription.
  └── Admin panel /webhooks → per-row Resume action. Dispatcher
      publishes a 'resumed' event on webhook_subscription_events,
      every worker re-evaluates the subscription, and dispatch
      starts from the new head of the queue.
```

## Procedure

Expected wall time: 15-45 minutes depending on whether you need a code fix or just a head-event drop.

### 1. Confirm the pause shape

Open admin panel `/webhooks`. The affected subscription shows a `paused` badge and `pause_reason` of `pending_ceiling`. The pending count column shows ~10,000 (or whatever the per-subscription ceiling is set to).

If `pause_reason` is `dlq_ceiling` instead, you are on the wrong runbook; use [the DLQ triage runbook](webhook-dlq-triage.md).

If `pause_reason` is `manual`, the subscription was paused by an admin via the PATCH endpoint, not by a ceiling auto-trigger; resume manually after confirming with whoever paused it.

### 2. Read the error log

Open `/webhooks` → `View errors` and filter to the affected subscription. The error log shows failed and DLQ rows with the catalog `short_message`. If the log is mostly empty for this subscription, the queue is stuck on its head event (no terminal failures yet); proceed to step 3.

If the log is mostly full of one error_kind, the consumer is rejecting a class of events and the right runbook is [DLQ triage](webhook-dlq-triage.md), not this one.

### 3. Identify the head event

Use the SQL in step 1 of the decision tree above. The result is the `event_id` blocking the queue. Pull the source from `integration_events`:

```sql
SELECT event_id, event_type, event_timestamp, aggregate_external_id,
       warehouse_id, source_txn_id, payload
  FROM integration_events
 WHERE event_id = <the-id>;
```

If the payload looks malformed or unexpected, you have an emit-side bug; that gets fixed via a code change in whichever handler emitted it (`api/services/picking_service.py`, `api/routes/receiving.py`, etc.) and a manual UPDATE on the row to repair the bad shape. That is a separate ticket beyond this runbook.

### 4. Inspect the in-progress rows for the head event

```sql
SELECT delivery_id, attempt_number, status, error_kind, error_detail,
       scheduled_at, attempted_at
  FROM webhook_deliveries
 WHERE subscription_id = '<sid>'
   AND event_id = <the-id>
 ORDER BY attempt_number ASC;
```

In a clean head-stuck scenario, you see 8 rows: 7 in `failed`, 1 in `pending` with `attempt_number=8` and a future `scheduled_at`. If you see 8 in `failed` and one in `dlq`, the head terminated but the queue's accumulated mass kept the pending count above the ceiling; resume on the next retry-slot tick (see step 6).

### 5. Decide and execute

Per the decision tree, pick one of:

**Option A: Drop the head.**

This is operator override, not a normal operation. Do this only when you are certain the event is not recoverable (consumer's verifier permanently rejects this shape; the event is duplicate of an earlier-applied one; the event refers to data that no longer exists). Manually:

```sql
UPDATE webhook_deliveries
   SET status = 'dlq',
       completed_at = NOW(),
       error_kind = COALESCE(error_kind, 'unknown'),
       error_detail = 'operator drop: pending-ceiling head unblock (incident <ticket-ref>)'
 WHERE subscription_id = '<sid>'
   AND event_id = <the-id>
   AND status IN ('pending', 'in_flight');

UPDATE webhook_subscriptions
   SET last_delivered_event_id = GREATEST(last_delivered_event_id, <the-id>)
 WHERE subscription_id = '<sid>';
```

The cursor advance on the second UPDATE is critical: without it, the dispatcher will pick up the same head event on resume and the queue stays stuck.

**Option B: Fix and replay.**

If the issue is a payload shape mismatch you can repair, UPDATE the `integration_events` row with the corrected payload, then either let the existing pending row's next retry slot fire, or issue a replay-one to insert a fresh attempt-1 row that will dispatch immediately on resume.

The audit row written for the manual UPDATE is your responsibility (no automatic audit_log write for direct DB UPDATEs); leave a structured comment with the ticket ref.

**Option C: Wait it out.**

If the consumer is recovering on its own and you expect the pending events to drain naturally, you can resume without dropping the head. The dispatcher will work through the queue at the rate the consumer accepts. The pending count will drop below the ceiling as in-flight rows terminate; the pause does not auto-clear, so step 6 (manual resume) is still required.

### 6. Resume the subscription

Admin panel `/webhooks` → per-row `Resume` action. The dispatcher publishes `resumed` on `webhook_subscription_events`; every worker re-evaluates the subscription and dispatches resume within sub-second on the Redis-pubsub path (60s on the fallback refresh).

Watch the pending count over the next 5 minutes:

- If it drops steadily, the queue is draining and you are clear.
- If it bounces back to ceiling within the same retry slot, the head event you thought you fixed is still failing. Pause again and re-investigate.

### 7. Capture the postmortem

Pending-ceiling fires are typically interesting incidents (a consumer suddenly slowed down, a payload bug shipped without coverage, the dispatcher fell behind for a tracked reason). Worth a writeup in whatever your team uses for postmortem records. Useful queries for the writeup:

```sql
-- Time the ceiling fired, plus the audit row capturing the auto-pause.
SELECT log_id, action_type, created_at, details
  FROM audit_log
 WHERE action_type = 'WEBHOOK_SUBSCRIPTION_UPDATE'
   AND details->>'subscription_id' = '<sid>'
   AND details->'diff'->'status'->>'after' = 'paused'
 ORDER BY log_id DESC
 LIMIT 5;

-- Pending count over time around the incident (run with hour
-- granularity from your Postgres history extension if you have one).
```

## Tuning the ceiling

`pending_ceiling` is per-subscription and constrained by the deployment-wide `DISPATCHER_MAX_PENDING_HARD_CAP` (default 50,000; env-var-only so an admin who can pause cannot also disable the safety ceiling). Per-subscription override is lower-bound only: you can shrink to 100, never grow above the hard cap.

When to lower below 10,000:

- Low-volume consumers where 10,000 pending represents months of backlog. Lower so the auto-pause fires before the operator forgets the subscription exists.
- Consumers with a strict latency contract where a sustained queue is itself a failure mode worth pausing on.

When to raise (up to the hard cap):

- High-volume consumers where 10,000 represents minutes of backlog and you want a longer head-stuck investigation window before auto-pause kicks in.
- Burst-tolerant consumers that can clear a 50,000-deep queue in under an hour.

`DISPATCHER_MAX_PENDING_HARD_CAP` lives in the dispatcher's environment block in `docker-compose.yml`. Changing it requires `docker compose up -d` (not `restart`) to re-read the env. Document the change in your deployment journal; the hard cap is the operator-controlled safety ceiling and increases should be deliberate.

## Recovery checklist

- [ ] Subscription resumed.
- [ ] Pending count back to steady-state range (under ~50 for a healthy consumer).
- [ ] Head event handled (dropped, fixed, or naturally drained).
- [ ] Audit log captures the resume.
- [ ] No new auto-pause within the next hour.
- [ ] Postmortem record captures the cause and any tuning changes made.
