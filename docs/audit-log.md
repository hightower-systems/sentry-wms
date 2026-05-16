# audit_log invariants

The `audit_log` table is the durable forensic record. Every admin
mutation site, every inbound write, every system-issued boot event
writes a row here. Three properties hold under all conditions and
are load-bearing for external auditor trust:

## 1. Append-only

`audit_log_reject_mutation` raises on UPDATE and DELETE. There is no
operator path to mutate or delete a row once committed. Schema
changes and migrations land via fresh INSERTs; cleanup of forensic
state requires `TRUNCATE CASCADE` (which itself is captured by the
forensic triggers on adjacent tables).

## 2. Per-row tamper-evidence

Every row carries `row_hash = sha256(prev_hash || canonical_payload)`
where `canonical_payload` is the concatenation of `action_type`,
`entity_type`, `entity_id`, `user_id`, `warehouse_id`, `details`, and
`created_at`. Modifying any of those columns retroactively breaks the
hash, detected by `verify_audit_log_chain()`.

## 3. Strict-by-log_id chain integrity, including under concurrent insert

For every row R with `log_id > 1`, `R.prev_hash` equals the row_hash of
the row with `log_id = R.log_id - 1`. Row 1's prev_hash is `\x00` (the
genesis anchor).

Pre-v1.7.0 #271 this property held only under sequential insert. Two
concurrent transactions would both `SELECT row_hash ... ORDER BY log_id
DESC LIMIT 1`, see the same prev_hash, compute distinct row_hashes for
distinct rows, and fork the chain. Per-row tamper-evidence still held
(each row's prev_hash referenced *some* prior row_hash and was sealed
at insert), but strict-by-log_id integrity broke.

v1.7.0 mig 047 fixes this with two related changes:

- The `audit_log.log_id` column drops its `BIGSERIAL DEFAULT`. The
  underlying sequence still exists; the BEFORE INSERT trigger calls
  `nextval` from inside its lock-protected critical section.
- The chain trigger acquires `LOCK TABLE audit_log_chain_head IN
  EXCLUSIVE MODE` at entry. Under PostgreSQL's MVCC contract, a
  waiting transaction reads the prior holder's committed UPDATE on
  unblock. With `nextval` also under the lock, log_id-order matches
  trigger-execution-order.

Two earlier iterations were tried and discarded:

1. `pg_advisory_xact_lock` alone: under READ COMMITTED, a PL/pgSQL
   trigger's SELECT inherits the parent INSERT statement's snapshot
   taken BEFORE the lock-wait. Even with serialized entry, the SELECT
   read stale prev_hash. Empirically still forked.
2. `SELECT FOR UPDATE` on a sentinel row: serialized the read
   correctly, but log_id allocation by the column DEFAULT happened
   before the trigger fired. Two concurrent inserts could obtain
   log_id=1 and log_id=2, then have their triggers run in reverse
   order under the lock. Chain held by trigger-execution-order but
   not by log_id-order.

The final form (`LOCK TABLE` + `nextval` inside the trigger) holds
under both shapes the pre-merge gate exercised:

- Boot-time burst: N parallel `_write_load_audit` calls from gunicorn
  workers loading mapping documents.
- Runtime burst: N parallel inbound POSTs each writing an
  `INBOUND_<RESOURCE>` row.

Regression coverage in `api/tests/test_audit_log_chain_concurrency.py`.

## Verification

`SELECT verify_audit_log_chain();` walks the entire table and asserts
both per-row tamper-evidence and strict-by-log_id chain integrity.
Returns the offending log_id on the first break, or NULL when the
chain is intact.

## inbound_source_systems_allowlist forensic shape (#275)

The `inbound_source_systems_allowlist_audit` table receives one row
per DELETE statement and one row per TRUNCATE statement. The DELETE
path is unconditional: any DELETE on the allowlist writes a forensic
row regardless of how many rows were deleted (zero-row DELETEs still
write).

The TRUNCATE path is **CASCADE-only**. Plain
`TRUNCATE inbound_source_systems_allowlist` raises
`ForeignKeyViolation` before the AFTER TRUNCATE trigger fires because
the v1.7 inbound tables (`inbound_sales_orders`, `inbound_items`,
`inbound_customers`, `inbound_purchase_orders`, `inbound_vendors`)
and `cross_system_mappings` carry NOT NULL FKs into
`source_system`, and `wms_tokens.source_system` carries a nullable
FK. Postgres refuses to truncate a table referenced by an FK without
CASCADE, so the trigger never executes on the plain form.

`TRUNCATE inbound_source_systems_allowlist CASCADE` is therefore the
sole path that writes a forensic audit row. An operator running a
direct plain TRUNCATE leaves a Postgres error in the server log but
no `inbound_source_systems_allowlist_audit` entry; investigators
auditing forensic state should treat the absence of an audit row as
"no successful TRUNCATE happened on this table" rather than
"someone bypassed the audit". The CASCADE path is what to look for
when reconstructing operator activity.

Regression coverage in
`api/tests/test_inbound_source_systems_allowlist_truncate.py`.

## Action constants and detail keys (v1.9.0)

Two new action constants land in v1.9.0:

- `ACTION_SHIP_VOID` -- a previously-shipped SO is reversed by a dockd
  POST `/void-ship`. Reverts the SO to `pre_ship_status` (PICKED or
  PACKED), reverses the matching `item_fulfillments` row, rolls back
  `sales_order_lines.quantity_shipped`. Emits `ship.voided/1` on the
  outbox.
- `ACTION_CANCEL` -- an SO is cancelled either by an admin or by an
  inbound source-system update detected as a cancel intent. Pre-PICK
  releases allocation; PICKED / PACKED reverts allocated and packed
  counters and returns inventory to the default receiving bin. Both
  surfaces delegate to `services.sales_order_service.cancel_sales_order`
  so the audit row shape is identical.

PICK / TO_LINE_PICKED / PACK / RECEIVE detail JSONB shapes gain
expected-vs-actual counters so an investigator can read cumulative
state from one row without joining upstream tables:

- PICK + TO_LINE_PICKED happy path: `quantity_to_pick` alongside
  `quantity_picked` (matches the keys SHORT_PICK already wrote).
- PACK: `total_expected` + `total_packed` alongside the existing
  `total_items` (preserved for any current consumer).
- RECEIVE: `quantity_ordered` + `quantity_received_before` alongside
  the existing `quantity` so ordered / previously-received / this-txn
  is reconstructable from one row.

Hash chain unaffected; only the JSONB payload shape changes. Older
rows keep their pre-v1.9 shape.

## Action constants and detail keys (v1.10.0)

Two new action constants land in v1.10.0:

- `ACTION_POS_CHECKOUT` -- a counter sale is committed via
  `POST /api/v1/pos/checkout`. Creates a `sales_orders` row with
  `status='SHIPPED'`, `order_source='pos'`, `order_type='sale'`;
  decrements `inventory.quantity_on_hand` per line; caches the
  response body on `sales_orders.cached_response_body` for replay.
- `ACTION_POS_REFUND` -- a counter sale is reversed via
  `POST /api/v1/pos/refund`. Creates a credit-memo `sales_orders`
  row with `order_type='refund'`, `parent_so_id` pointing at the
  original; re-increments `inventory.quantity_on_hand` per line;
  marks the original SO `refunded_at` + `refund_so_id`.

Both rows carry `entity_type='SO'`. `entity_id` is the integer
`so_id` of the SO the row attributes to: the freshly-created sale
SO for `POS_CHECKOUT`, the freshly-created credit-memo SO for
`POS_REFUND`. `user_id` is the wire-level `cashier_id` from the
request body (the POS Service's own user-table id; never FK'd to
`users` -- POS sales attribute to a cashier identity that lives
outside Sentry). `warehouse_id` is the SO header warehouse.

`details` JSONB shape per row:

- `POS_CHECKOUT`: `idempotency_key`, `external_txn_ref` (the
  Windcave DpsTxnRef or cash sale reference), `terminal_id`,
  `so_number`, `total_cents`, `payment_method` (`card` or `cash`),
  `header_warehouse` (the `warehouse_code`), and
  `lines: [{sku, warehouse_id, bin_id, quantity, unit_price_cents,
  tax_cents, line_total_cents}]`. Pricing fields live only in the
  audit details; `sales_order_lines` carries no per-line price
  columns in v1.10. The refund route reads this audit row to
  recover the original allocation locations for the re-increment
  step.
- `POS_REFUND`: `idempotency_key`, `external_refund_ref` (the
  refund leg's payment reference), `original_external_txn_ref` (the
  original sale's reference, captured for forensic cross-check),
  `original_so_id` (the original sale's `so_number`),
  `refund_so_number` (the credit-memo's `so_number`), `terminal_id`,
  `total_cents`, `payment_method`, and `lines` (the original
  sale's line locations, copied verbatim from the original
  `POS_CHECKOUT` row's details so the refund's audit entry is
  self-contained).

Hash chain extends through every new write; `verify_audit_log_chain()`
continues to pass with the additions.
