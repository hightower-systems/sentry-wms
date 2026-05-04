# Webhook DLQ triage runbook

Audience: operators investigating dead-lettered webhook deliveries.

Scope: how to read the DLQ viewer, the replay-one vs replay-batch decision tree, the auto-pause unblock procedure, and how to spot patterns that signal a deeper incident.

## What the DLQ is

Every webhook delivery has up to eight attempts. After the eighth failure, the dispatcher flips the row's status to `dlq` (no ninth row is inserted; the eighth is mutated in place) and advances the subscription's cursor. The DLQ row stays in `webhook_deliveries` for 90 days (the cleanup beat from #194 reaps terminal rows on the daily sweep) and is visible in the per-subscription DLQ viewer at admin panel `/webhooks` → per-row `DLQ` action.

A DLQ row is the recoverable failure surface. Sentry retried up to its documented schedule and gave up; the operator decides whether to drop the event, fix the consumer and replay, or replay-batch the rest of the affected window.

## When to triage

The DLQ ceiling (default 1,000) auto-pauses the subscription with `pause_reason='dlq_ceiling'`. Past that, the subscription stops dispatching entirely until you triage. Practical triage triggers:

- The DLQ count for a subscription climbs past ~10 in a sustained way. One-off DLQ rows are noise; sustained climb is signal.
- The subscription auto-paused (visible as a `paused` status badge with `pause_reason=dlq_ceiling` on the `/webhooks` list).
- A consumer reports they saw a fix go in but Sentry is not retrying old failures (correct: Sentry does not auto-retry DLQ rows; replay is operator-initiated).
- The cross-subscription error log at `/webhooks` → `View errors` shows a sudden shift in `error_kind` distribution (e.g. a wave of `tls` errors after a consumer cert rotation).

## Reading the DLQ viewer

Open the admin panel `/webhooks` page and click the per-row `DLQ` action on the affected subscription. The viewer paginates with adjustable page size (25 / 50 / 100 / 250; the server caps at 500). Each row shows:

| Column | Meaning |
|--------|---------|
| Delivery | `delivery_id`, the per-attempt PK. Replay-one targets this column. |
| Event | `event_id`, the `integration_events` PK the delivery was carrying. Stable across retries; this is the consumer-facing dedupe key. |
| Type | `event_type` (joined from `integration_events`). |
| Attempt | Always `8` for DLQ rows (the eighth failure is the terminal transition). |
| HTTP | The HTTP status code from the consumer's last response. `null` for network-level failures (timeout, TLS, SSRF reject). |
| Error | `error_kind` from the catalog (`timeout`, `connection`, `tls`, `4xx`, `5xx`, `ssrf_rejected`, `unknown`). |
| Detail | The catalog `short_message` for the kind. Hover for the longer description. **No bytes from the consumer's response body are stored or shown.** |
| Completed | When the eighth attempt landed (or failed). |
| Gen | Which secret generation the delivery signed with. Useful when investigating a rotation cutover. |

Click a row's `Replay` action to insert a fresh `pending` row pointing at the same `event_id`. The original row stays in place as the audit trail; the cursor is not touched (replay does not advance `last_delivered_event_id`).

For a deeper inspection of what payload failed without a second round-trip, the DLQ endpoint already joins to `integration_events` and serves the source event context (warehouse, source_txn_id, aggregate_external_id). The viewer renders this in the row's expanded view via the cross-subscription error log at `/webhooks` → `View errors`, which surfaces the catalog `description` and `triage_hint` per row.

## Replay-one vs replay-batch decision tree

```
Is the failing event a one-off?
  └── Yes: replay-one. Consumer applies it and you move on.
  └── No, it is a class of events failing the same way.
        ├── Did you fix the underlying cause?
        │     ├── Yes: replay-batch with the matching filter (status=dlq + the
        │     │       relevant event_type / warehouse_id / completed_at window).
        │     │       Server computes the impact estimate and surfaces it
        │     │       inline; if the batch exceeds the 10,000-row cap, the
        │     │       confirmation modal asks you to acknowledge.
        │     └── No: do not replay. The cause will recur and you will
        │             multiply DLQ rows. Pause the subscription if it is
        │             still active to stop new failures piling up; fix the
        │             cause first.
        └── Are the events still failing for an external reason
            you cannot fix (e.g. the consumer's upstream is down)?
              └── Wait. Resume / leave paused. The 90-day retention
                  window is your budget. When the upstream returns,
                  replay-batch the affected window.
```

Replay-one is the right tool for individual investigation: you fix one cause, replay one event, confirm the consumer applied it correctly, then decide whether the same fix unlocks the rest of the DLQ.

Replay-batch is the right tool when you have proven the fix on one event and want to fan it out. The server computes the impact estimate from the filter; if it exceeds `DISPATCHER_REPLAY_BATCH_HARD_CAP` (default 10,000), the modal swaps the action button to "Acknowledge and replay N" and requires explicit acknowledgement. The 60-second per-subscription throttle protects against accidental double-fires.

## Auto-pause unblock procedure

When you open `/webhooks` and see a row badged `paused` with `pause_reason='dlq_ceiling'` (or `pending_ceiling`; see [the pending-ceiling runbook](webhook-pending-ceiling.md) for that flavour):

1. **Open the DLQ viewer for the subscription.** Read the most recent ~50 rows.
2. **Identify the dominant `error_kind`.** If most rows share one kind, the failure has a common cause; fix that first.
3. **Fix the underlying cause.** Common shapes:
   - Sustained `tls` after a date: consumer rotated a certificate without renewing. Operator side: nothing to do; consumer fixes.
   - Sustained `4xx` with a clear deploy time: consumer deployed a verifier change. Operator side: confirm the verifier matches the docs at `docs/api/webhooks.md`.
   - Sustained `connection` or `timeout`: consumer endpoint is overloaded or down. Operator side: nothing to do; wait for consumer recovery.
   - Sustained `ssrf_rejected`: the consumer's DNS resolved to a private IP (DNS rebinding caught at dispatch time, or the consumer's domain points at an internal address). Operator side: confirm the delivery URL is correct.
4. **Decide on the existing rows.** Replay-batch with `status=dlq` if the fix is confirmed; leave them if you cannot prove the fix; drop them (operator note: there is no "drop DLQ" button; you wait for the 90-day cleanup beat) if they are unprocessable.
5. **Resume the subscription.** Open the per-row edit / pause modal and click `Resume`. The dispatcher publishes a `resumed` event on `webhook_subscription_events` and picks up where the cursor left off (the cursor stayed put while paused; no events were lost).
6. **Watch for re-trigger.** Open the cross-subscription error log at `/webhooks` → `View errors` filtered by the subscription. If new DLQ rows accumulate within the next ~hour, the underlying cause is not fully fixed; pause again and repeat.

## Spotting deeper patterns

Use the cross-subscription error log (`/webhooks` → `View errors`) when a single subscription's DLQ does not tell the full story:

- **Multiple subscriptions, same `error_kind`, narrow time window** = something on the Sentry side. Likely candidates: dispatcher container restart with a misconfigured env var, network-level outage between Sentry and the public internet, Fernet key issue affecting `webhook_secrets` decrypt.
- **Multiple subscriptions, multiple `error_kind`, narrow time window** = also Sentry-side, probably a dispatcher restart with mixed environment.
- **One subscription, one `error_kind`, sustained over hours** = consumer-side. Hand off to the consumer's owner.
- **One subscription, multiple `error_kind`, sustained** = consumer's endpoint is unstable. Hand off and pause the subscription if the DLQ is climbing toward the ceiling.

## Replay-batch impact estimate semantics

The server computes the impact estimate from the same SQL clause that drives the actual replay, so the count the modal shows is the count the replay will produce:

```sql
SELECT COUNT(*) FROM webhook_deliveries d
  LEFT JOIN integration_events e ON e.event_id = d.event_id
 WHERE d.subscription_id = :sid
   AND d.status = :status
   AND <event_type / warehouse_id / completed_at window clauses>
```

The replay-batch then INSERTs that many fresh `pending` rows in a single statement. The 60-second throttle is enforced via `audit_log`, so a missed-trigger restart cannot reset the timer.

If the impact estimate is `0` after submit, the modal shows "Replayed 0 deliveries" and writes a zero-impact audit row anyway (visible in `audit_log` under `action_type=WEBHOOK_DELIVERY_REPLAY_BATCH`). A zero-impact filter is operator intent worth recording.

## Audit trail review

Every replay (single or batch) writes an `audit_log` row. After a triage session, confirm:

```sql
SELECT log_id, action_type, user_id, created_at, details
  FROM audit_log
 WHERE action_type IN ('WEBHOOK_DELIVERY_REPLAY_SINGLE',
                        'WEBHOOK_DELIVERY_REPLAY_BATCH')
   AND details->>'subscription_id' = '<the-uuid>'
   AND created_at > NOW() - INTERVAL '1 day'
 ORDER BY log_id DESC;
```

`WEBHOOK_DELIVERY_REPLAY_BATCH` `details` carries the filter object and the impact_count, so you can reconstruct exactly which rows were targeted.

## Recovery checklist

After triaging a DLQ wave:

- [ ] Underlying cause identified (consumer fix, dispatcher fix, or external dependency recovery).
- [ ] Replays issued where the fix is confirmed.
- [ ] Subscription resumed if it was auto-paused.
- [ ] No new DLQ growth in the cross-subscription error log over the next hour.
- [ ] `audit_log` shows the replay rows for any replayed events.
- [ ] If the cause was Sentry-side, the dispatcher logs (`docker compose logs sentry-dispatcher`) reflect the recovery (no recurring stack traces, healthcheck heartbeat fresh).
