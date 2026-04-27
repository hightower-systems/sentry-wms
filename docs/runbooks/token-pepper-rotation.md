# Token pepper rotation runbook

Audience: operators who need to rotate `SENTRY_TOKEN_PEPPER` after a credential leak or on a scheduled interval.

Scope: what the pepper is, when to rotate, the exact procedure, and the blast radius to expect.

## What the pepper is

`SENTRY_TOKEN_PEPPER` is an environment variable concatenated with every inbound X-WMS-Token plaintext before the SHA-256 hash step:

```
token_hash = SHA256(pepper || plaintext).hexdigest()
```

It is stored only in the `.env` file (forwarded to the `api` and `snapshot-keeper` containers via `docker-compose.yml`). It is never written to the database. If an attacker obtains a copy of the `wms_tokens` table but not the pepper, they cannot brute-force the plaintexts (the pepper's 32 bytes of entropy makes the dictionary search space infeasible). If they obtain both, they can.

Rotating the pepper invalidates every issued `wms_tokens` row at once (stored hashes no longer match any reachable plaintext). This is **by design**: the rotate is the emergency control you pull when the hash table leaks.

## When to rotate

**Do rotate:**

- The `wms_tokens` table or a DB backup was exfiltrated or exposed.
- An ex-employee or ex-contractor retained access to production secrets (including the pepper itself).
- A process-listing leak or log-leak exposed the env var.

**Do not rotate:**

- On a calendar schedule for its own sake. The pepper adds no security over time; it is a one-shot control that fires once per incident. Frequent rotation is operational cost without a security benefit, and it churns every connector's token configuration every cycle.
- Because a single token leaked. Revoke the leaked token via the admin panel (`/api-tokens` → red revoke button) instead. Revocation is visible across every worker within sub-second wall time: admin mutations publish on the `wms_token_events` Redis pubsub channel and every worker's subscriber thread evicts the cached entry on receipt. The 60-second per-worker TTL remains only as a backstop for the Redis-unavailable path.

## Procedure

The rotate is disruptive -- every connector loses authentication -- so plan a maintenance window. Expected wall time: 10-20 minutes depending on how many connectors you need to re-issue to.

### 1. Communicate the window

Notify every downstream connector owner. Every X-WMS-Token they hold will stop working the moment the api container restarts with the new pepper.

### 2. Generate the new pepper

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Save the output in a password manager. You will need it again when re-issuing tokens.

### 3. Stage the update

Edit `.env` at the repo root (Compose deployment) and replace the existing `SENTRY_TOKEN_PEPPER=...` line with the new value.

Do not run `docker compose restart`; it does not re-read `.env`. You must use `up -d` which recreates the containers.

### 4. Apply

```bash
docker compose down
docker compose up -d
```

Both `api` and `snapshot-keeper` pick up the new pepper on boot. The admin panel's browser session (cookie auth) is unaffected.

### 5. Verify the rotate took effect

```bash
# Every previously-issued token now hashes to a value that is NOT in
# wms_tokens. The decorator returns 401 invalid_token.
curl -i http://localhost:5000/api/v1/events/types \
  -H "X-WMS-Token: <some-previously-working-token>"
# Expected: HTTP/1.1 401 Unauthorized  {"error":"invalid_token"}
```

### 6. Re-issue every connector token

Log into the admin panel, go to `/api-tokens`, and issue fresh plaintexts for every connector that needs continued access. Copy each plaintext out of the one-time reveal modal and hand it to the connector's operator through your secure channel.

### 7. Clean up revoked / expired rows

After every connector has a fresh token and you've confirmed their polls are landing, delete the stale rows from `wms_tokens` (they carry no security risk since the hashes no longer resolve, but they clutter the admin list):

```
/api-tokens → trash-icon the old rows
```

Or bulk delete via SQL if the count is large:

```sql
-- Target rows created before the rotation timestamp; leave the fresh
-- post-rotation rows alone.
DELETE FROM wms_tokens WHERE created_at < '2026-MM-DD HH:MM:SS+00'::timestamptz;
```

## Blast radius

- **Connectors:** every X-WMS-Token stops authenticating the moment the api container restarts. Connectors that are actively polling will get 401s on their next poll. Connectors that are mid-snapshot will get 410 Gone on their next page (the snapshot-keeper's held scan rows reference `created_by_token_id`, but the token lookup now fails).
- **Admin panel:** unaffected. Cookie auth does not use the pepper.
- **Mobile (Chainway C6000):** unaffected. Mobile talks to `/api/receiving/*`, `/api/picking/*`, etc. with JWT + cookie auth; it never sees the v1.5.0 `/api/v1/*` surface.
- **Outbox emission:** unaffected. The seven emission sites write directly to `integration_events` inside the handler's DB transaction; no token auth involved.

## What you do NOT need to do

- You do not need to invalidate or re-issue JWTs. JWT signing is gated on `JWT_SECRET`, a separate secret.
- You do not need to re-run migrations. The pepper is a runtime secret, not a schema concern.
- You do not need to wipe connector-side state (cursors, subscription filters). Consumer groups persist across rotations; only the token used to authenticate the polls changes. After re-issue the connector resumes from its stored `last_cursor`.

## Future: pepper-per-era

If you need to rotate the pepper without invalidating every token at once, the architecture that supports that lives in v2.x: a `pepper_generation` column on `wms_tokens` so the decorator can try multiple peppers in order (newest first) and rehash on first successful authentication. Out of scope for v1.5.0.
