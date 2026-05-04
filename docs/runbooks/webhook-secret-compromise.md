# Webhook secret compromise runbook

Audience: operators responding to a leaked or compromised webhook HMAC secret.

Scope: what the per-subscription webhook secret is, when to rotate, the rotation procedure, the 24-hour dual-accept window's blast radius, and the audit-trail review steps after the incident.

## What the secret is

Every webhook subscription has its own HMAC-SHA256 shared secret stored encrypted in `webhook_secrets`. The dispatcher decrypts the current generation (gen=1) at signing time and signs every outbound POST as:

```
HMAC-SHA256(secret, f"{X-Sentry-Timestamp}.{body}")
```

The consumer's endpoint verifies the same input with the same secret. If the secret leaks, an attacker can forge POSTs to the consumer's endpoint that pass signature verification and replay-protection checks. The consumer's downstream handlers then trust the forged payload.

The secret is per-subscription, not deployment-wide. Compromise of one subscription's secret does not affect any other subscription. Compromise of the Fernet master key (`SENTRY_ENCRYPTION_KEY`), however, exposes every encrypted secret at once; that scenario is handled by the [Fernet rotation runbook in `docs/connectors.md`](../connectors.md), not this one.

## When to rotate

**Do rotate:**

- A consumer's secret store was exfiltrated, logged, screenshotted, or otherwise exposed.
- The consumer suspects compromise but has no positive evidence (precautionary rotation is cheap; do it).
- An ex-employee or ex-contractor retained access to the consumer's secret store.
- A failed signature verification spike on the consumer side suggests an attacker is brute-forcing or replaying.

**Do not rotate:**

- On a calendar schedule for its own sake. The 24-hour dual-accept window is a one-shot control for compromise scenarios; rotating without cause churns the consumer's secret store and shortens any future cutover window if a real compromise lands during the recovery period.
- Because a single delivery hit the DLQ. Use the [DLQ triage runbook](webhook-dlq-triage.md) instead.
- Because the consumer's signature verifier is mis-implemented. Fix the verifier first; rotation does not solve a broken verifier.

## What rotation does

Rotation runs three steps atomically inside one DB transaction:

1. Any older gen=2 row for this subscription is dropped.
2. The current gen=1 is demoted to gen=2 with `expires_at = NOW() + INTERVAL '24 hours'`.
3. A fresh 32-byte URL-safe plaintext is encrypted as the new gen=1 with `expires_at = NULL`.

The dispatcher signs every subsequent POST with the new gen=1 immediately. The consumer's verifier MUST accept either generation until the gen=2 `expires_at` passes; after that, only gen=1 verifies. The consumer therefore has 24 hours to update its secret store before deliveries signed with gen=1 alone start failing on the consumer side.

A `secret_rotated` event publishes on the `webhook_subscription_events` Redis pubsub channel; every dispatcher worker refreshes its cached signing key from DB before the next dispatch on this subscription. The 60-second subscription-list refresh remains as the backstop when Redis is unavailable.

## Procedure

Expected wall time: 5-10 minutes plus whatever the consumer needs to update their secret store.

### 1. Confirm the subscription scope

Open the admin panel `/webhooks` page and identify the affected subscription by display name + connector. Note the `subscription_id` (visible in the URL when you open the detail / edit modal); you will need it for the audit-trail review in step 6.

If multiple subscriptions share the same downstream consumer's secret store (e.g. one consumer terminating multiple subscriptions in one verifier), rotate every subscription that shares the compromised store. The dual-accept windows run independently per subscription, so the consumer's verifier needs to track each generation separately.

### 2. Notify the consumer's owner

Tell the consumer:

- Which subscription you are rotating (display name + delivery URL).
- Approximate clock time of the rotation.
- The 24-hour deadline by which their secret store must hold both old and new generations.

The consumer should be ready to receive the new plaintext and write it to their secret store before you click rotate. Sentry shows the plaintext exactly once; if you click rotate and the consumer is not ready, you have to rotate again to issue another plaintext, and any second rotation within 24 hours overwrites the demoted secret and shortens the cutover.

### 3. Click rotate

In the admin panel `/webhooks` page, click the per-row `Rotate` action on the affected subscription. The confirmation modal explains the 24-hour dual-accept window. Click `Rotate`.

The reveal modal appears with the new plaintext and a copy-to-clipboard button. The plaintext is shown exactly once; closing the modal without saving means another rotation cycle.

### 4. Hand the plaintext to the consumer

Use whatever channel your organization uses for secret material (HashiCorp Vault, 1Password vault, encrypted email; never plain Slack). The consumer:

1. Moves the value at slot 1 in their secret store to slot 2.
2. Writes the new plaintext to slot 1.
3. Verifies their verifier reads both slots based on the `X-Sentry-Signature-Generation` header.

The consumer integration guide at [`docs/api/webhooks.md`](../api/webhooks.md) has reference verifiers in Python and Node showing the slot-aware secret lookup.

### 5. Confirm dispatch with the new generation

Trigger an event that matches the subscription's filter (or wait for one to flow naturally) and check the consumer's logs. The request should arrive with `X-Sentry-Signature-Generation: 1` and verify against the new plaintext.

If the consumer 401s after rotation, the consumer's verifier is missing the new plaintext or is failing the generation lookup. Do not rotate again; fix the consumer's secret store. The dispatcher will retry on the documented eight-attempt schedule, so a brief consumer-side fix window is recoverable without DLQ growth.

### 6. Review the audit trail

Every rotation writes an `audit_log` row with `action_type=WEBHOOK_SECRET_ROTATE`. Confirm the row shows:

- The `subscription_id` you rotated.
- The `user_id` matches the admin who clicked rotate.
- `details.demoted_prior_primary` is `true` (every rotation except the very first issuance demotes a prior gen=1).
- The `created_at` timestamp matches your wall-time expectation.

```sql
SELECT log_id, action_type, user_id, created_at, details
  FROM audit_log
 WHERE action_type = 'WEBHOOK_SECRET_ROTATE'
   AND details->>'subscription_id' = '<the-uuid-from-step-1>'
 ORDER BY log_id DESC
 LIMIT 5;
```

The hash chain is verified by `verify_audit_log_chain()` (v1.4 hardening, carried forward through v1.5 / v1.6); a tampered row would surface there. If the chain reports a mismatch around your rotation timestamp, treat that as a separate incident: the audit log itself has been altered.

### 7. Wait the full 24 hours before rotating again

A second rotation within the dual-accept window overwrites gen=2 and shortens the cutover for any consumer-side endpoint that has not yet picked up the new value. The runbook for compromise scenarios specifically advises waiting unless a SECOND compromise lands in the same window.

If a second compromise does land in the window, the cutover is necessarily shorter. Expect the consumer's reaction time to be the limiting factor and pre-stage their secret-store update before clicking the second rotate.

## Blast radius

- One subscription's secret rotates. Other subscriptions are unaffected.
- The dispatcher signs new POSTs with gen=1 immediately on the next dispatch cycle (sub-second on the Redis-pubsub path; up to 60 seconds on the fallback refresh).
- The consumer has 24 hours to honour both generations. After that, gen=2 stops verifying and the cleanup beat will reap the gen=2 row from `webhook_secrets` on its next hourly tick.
- No deliveries are dropped during the rotation. In-flight deliveries continue with whatever generation the dispatcher had when they started; new deliveries pick up the new gen=1.
- Replay-batches issued during the rotation use whatever generation is current at the time the replay POST goes out, not the generation the original delivery used.

## Recovery checklist

After the incident, before closing the ticket:

- [ ] All affected subscriptions rotated.
- [ ] Consumer confirmed both generations work in their verifier (one synthetic test per subscription).
- [ ] `audit_log` shows the `WEBHOOK_SECRET_ROTATE` row for each rotation.
- [ ] Hash chain verifies clean across the rotation timestamps.
- [ ] If the leak source is known (e.g. an exposed secret in a git repo), the upstream source is also remediated (force-push removal does not retroactively unleak; rotate AND remove).
- [ ] The 24-hour cutover passed without DLQ growth on the affected subscriptions (check the cross-subscription error log at `/webhooks` → "View errors" with the `secret_rotated` window).

If DLQ rows accumulate during the cutover, the consumer's verifier is rejecting requests it should accept. Pause the subscription, triage with the [DLQ triage runbook](webhook-dlq-triage.md), and resume when the consumer is fixed. Do not rotate again without a confirmed second compromise.
