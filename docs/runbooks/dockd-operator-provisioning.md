# Dockd operator provisioning

Audience: operators rolling out the v1.9 dockd integration and onboarding new shipping-station staff.

Scope: a one-time-per-operator setup so a dockd station's `operator_username` resolves to a Sentry `users` row. Without this, the first ship attempt fails with `422 unknown_operator` and the operator's UI shows "user not provisioned in Sentry, contact admin."

## Why this matters

Dockd has its own user table (username + password). When a station POSTs `/api/v1/dockd/orders/<so>/ship`, the body's `operator_username` field is a free-text string. Sentry's ship handler looks the username up in its own `users` table to:

- Store the username on `audit_log.user_id` so the audit chain attributes the ship to a specific operator.
- Store the username on `item_fulfillments.shipped_by` so the ship history is operator-attributable.
- Resolve the user's `external_id` (UUID) for the `ship.confirmed/1` event payload's `completed_by_user_external_id` field, which Fabric and other downstream consumers use to map ships back to people.

If the username is missing in Sentry's `users` table, all three of those data flows fail. The handler returns `422 unknown_operator` rather than silently storing an attribution-less ship.

## When to provision

Once per operator, BEFORE that operator runs their first ship at a dockd station. AvidMax's operator turnover is roughly quarterly, so this fits in the same admin-task cadence as W-2 onboarding.

If an operator hits the 422 mid-shift: provision them via the procedure below, ask them to retry the same scan (idempotency_key is fresh per UI action; the same SO will still be in PICKED/PACKED waiting). No data loss.

## Procedure (admin UI)

1. Open the admin Users page.
2. Click "Create user."
3. Fill in:
   - **username**: must EXACTLY match the operator's dockd login username (case-sensitive). The dockd-side and Sentry-side usernames are the join key; a mismatch is an unknown_operator.
   - **password**: any value. The operator never logs into Sentry directly; dockd handles their authentication. Generate something random (e.g., `openssl rand -base64 24`) and discard it; the password column is `NOT NULL` at the schema level so a placeholder is required.
   - **full_name**: human-readable for audit-log readability ("Mike Hightower" rather than the username).
   - **role**: `USER` (not `ADMIN`). Dockd operators do not need admin-panel access. The role gate stops them from using their Sentry credentials to reach `/api/admin/*` even if they obtained the password.
   - **warehouse_ids**: at least the warehouse(s) they ship from. v1.9 dockd auth is token-bound to a warehouse, but the user record's warehouse list keeps the dashboard / reporting filters consistent.
   - **allowed_functions**: leave empty for dockd-only operators. The mobile-app modules don't apply.
4. Save. Sentry assigns `external_id` (UUID) automatically.

## Procedure (SQL fallback)

For headless / scripted onboarding, or when the admin UI has not been deployed:

```sql
INSERT INTO users (username, password_hash, full_name, role, external_id)
VALUES (
    'mike',                                                    -- match dockd username
    'placeholder-not-used-{REPLACE_WITH_RANDOM}',              -- bcrypt hash a random throwaway
    'Mike Hightower',
    'USER',
    gen_random_uuid()
)
ON CONFLICT (username) DO NOTHING;
```

The `password_hash` column requires a non-NULL value. A safe "operator never logs in" placeholder:

```bash
python3 -c "import bcrypt, secrets; print(bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt()).decode())"
```

Pipe that into the INSERT in place of `placeholder-not-used-...`.

The `ON CONFLICT (username) DO NOTHING` clause makes the SQL idempotent if you re-run the script. The audit_log hash chain extends automatically; the V-208 chain trigger picks up the row mutation.

## Verification

After provisioning, verify with one shipping flow at the operator's station:

1. Operator logs into dockd.
2. Operator scans an order in PICKED/PACKED status.
3. Operator clicks Ship; ShipRush returns a tracking number; dockd POSTs `/api/v1/dockd/orders/<so>/ship`.
4. Sentry returns 200 with `audit_log_id` populated. The dockd UI advances to the printed-label state.

If step 4 returns `422 unknown_operator`, double-check the username is byte-equal between dockd and Sentry. The most common cause is a leading/trailing space or a capitalization mismatch.

## Decommissioning

When an operator leaves:

1. Set `is_active = FALSE` on their Sentry users row (do NOT delete -- audit_log rows reference the username string and the row preserves the hash chain).
2. Disable their dockd login.

Sentry's `audit_log.user_id` retains the username string indefinitely; historical ship attribution is preserved even after the row is deactivated.

```sql
UPDATE users SET is_active = FALSE WHERE username = 'mike';
```

## Future work (out of scope for v1)

Two automation candidates flagged in the dockd plan:

- **`POST /api/v1/admin/users/sync-from-dockd`**: pulls the dockd user list and provisions matching Sentry rows in one call. Worth the day's work if operator turnover ever exceeds quarterly.
- **Login-time `whoami` probe**: dockd issues a lightweight Sentry call at operator login that authenticates the operator and surfaces unknown_operator at login rather than mid-ship. Requires a Sentry-side endpoint that authenticates an operator without an order context. Lives on the dockd-side spec.

Both deferred until production cadence justifies the effort.
