"""HMAC-SHA256 signing + 24-hour dual-accept rotation.

Filled in by D2. Canonical signing input is ``f\"{timestamp}.{body}\"``
where ``body`` is the bytes of the envelope serialized exactly
ONCE via ``json.dumps(envelope, separators=(\",\", \":\"), sort_keys=True).encode(\"utf-8\")``.
Plan §3 prescribes:

  * Single-serialization invariant enforced by lint + runtime
    assertion + e2e test.
  * Constant-time verification via ``hmac.compare_digest``.
  * Plaintext lifecycle: decrypt into a local variable inside this
    module, never pass up the call stack; ``repr()`` of the
    secret wrapper does not echo plaintext.
"""
