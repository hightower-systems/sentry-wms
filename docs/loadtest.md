# v1.7.0 inbound load test

This is the operator runbook for `tools/loadtest/inbound_v1_7.js`,
the k6 script that exercises the five Pipe B inbound endpoints
(`sales_orders`, `items`, `customers`, `vendors`, `purchase_orders`)
with realistic source payloads under concurrent load.

The script is **operator-run, not part of CI**. Run it from a
workstation against a staging stack pre-merge to confirm pre-merge
gate 25 ("v1.7 inbound endpoints sustain a realistic burst without
5xx leakage or chain-fork regressions") still holds. The chain
serialization fix (#271) and the body-cap boot guard (#273) cover
correctness; this runbook covers throughput and tail latency.

## Prerequisites

1. **k6 installed** on the runner machine. The Linux / macOS install:

   ```sh
   # macOS
   brew install k6
   # Debian / Ubuntu
   sudo gpg -k && \
     sudo gpg --no-default-keyring \
       --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
       --keyserver hkp://keyserver.ubuntu.com:80 \
       --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69 && \
     echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] \
       https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list && \
     sudo apt-get update && sudo apt-get install k6
   ```

2. **Sentry stack reachable** at `SENTRY_BASE_URL` (default
   `http://localhost:5000`). The stack must have the v1.7 schema
   loaded (mig 047 + mig 048) and a valid mapping doc on disk for
   `SENTRY_SOURCE_SYSTEM`. Boot fails loudly otherwise.

3. **A WMS token issued** with:
    - `inbound_resources` covering the five resources (or a subset
      matching what the script targets; the default script targets
      all five).
    - `source_system` matching `SENTRY_SOURCE_SYSTEM`.
    - `mapping_override` left **off** (v1.7.0 rejects requests with
      `mapping_overrides` regardless; #269).

   Issue via `POST /api/admin/tokens` from the admin panel; copy the
   plaintext returned in the response into `SENTRY_WMS_TOKEN`.

4. **Allowlist row** for `SENTRY_SOURCE_SYSTEM` in
   `inbound_source_systems_allowlist` (the boot guard fails loud
   otherwise; see #267).

## Running

Default profile (5 VUs warmup, 20 VUs realistic peak, 30s cooldown):

```sh
SENTRY_BASE_URL=http://localhost:5000 \
SENTRY_WMS_TOKEN=<plaintext> \
SENTRY_SOURCE_SYSTEM=acme-erp \
k6 run tools/loadtest/inbound_v1_7.js
```

Stress profile (10 VUs warmup, 50 VUs peak, 30s cooldown) for
regression hunting:

```sh
LOADTEST_PROFILE=stress \
SENTRY_BASE_URL=http://localhost:5000 \
SENTRY_WMS_TOKEN=<plaintext> \
SENTRY_SOURCE_SYSTEM=acme-erp \
k6 run tools/loadtest/inbound_v1_7.js
```

The script writes `loadtest-summary.json` alongside the standard
stdout summary so trend tracking across runs is straightforward.

## Thresholds

Default profile:

| Metric                                    | Threshold |
|-------------------------------------------|-----------|
| `http_req_failed{kind:5xx}` rate          | < 0.1 %   |
| `http_req_duration{endpoint:*}` p95       | < 300 ms  |
| `http_req_duration` aggregate p99         | < 1000 ms |

Stress profile relaxes to p95 < 800 ms and p99 < 2000 ms because
the 50-VU shape contends for the gunicorn worker pool.

## Investigating threshold trips

A failed run does not by itself indicate a regression -- a slow
laptop, network jitter, or a stack mid-deploy can all push p95.
Triage in this order:

1. **5xx rate > 0**. Check the stack's audit_log + application log
   for the request that failed. A 500 inside the chain trigger is
   the highest-priority signal -- mig 047 should serialize the
   inserts; a single 500 here suggests a fork escape and warrants
   a `verify_audit_log_chain()` run.
2. **p95 trip on a single endpoint**. Likely a mapping-doc shape
   issue (a derived expression scanning a large array, a
   cross-system lookup hitting a cold index). Profile the
   handler against the same payload outside k6.
3. **p99 trip aggregate, p95 fine**. Tail latency in the connection
   pool or the audit_log lock. Confirm with
   `pg_stat_activity` during a re-run and look for trigger waits
   on `audit_log_chain_head`.

## Expected baselines (apartment-lab fixtures, default profile)

These are reference numbers from the v1.7.0 pre-merge gate; treat
them as "if this run matches, gate 25 holds" rather than as hard
contracts.

| Metric                                | Apartment-lab baseline |
|---------------------------------------|------------------------|
| Total iterations across the 120s run  | 6,000-8,000           |
| `http_req_duration` p95 aggregate     | 80-150 ms              |
| `http_req_duration` p99 aggregate     | 200-400 ms             |
| `http_req_failed{kind:5xx}` rate      | 0 %                    |

Stress profile typically lands p95 around 250-400 ms with no 5xx
on a 4-worker gunicorn stack. A 5xx rate above zero in the stress
profile is also a regression worth investigating.

## When to run

- Before every v1.7.x merge to `main` -- gate 25 is mandatory.
- After any change to:
    - `services.inbound_service` (handler hot path)
    - `services.mapping_loader` (`apply()` is on the hot path)
    - `db/migrations/047_audit_log_chain_serialization.sql` (or any
      audit_log trigger change)
    - `services.token_cache` (auth hot path)
- After any Postgres upgrade or change to gunicorn worker count /
  connection pool size.

## Why this is operator-run, not in CI

CI runs in shared GitHub Actions infrastructure; load test results
from a shared runner with unpredictable neighbor noise are unstable
enough to be net-misleading. A dedicated workstation against a
staging stack with consistent neighbor profile produces a number
that's actually comparable run-over-run.
