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
