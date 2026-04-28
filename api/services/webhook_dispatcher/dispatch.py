"""Per-subscription delivery loop.

Filled in by D5. One thread per active subscription, refreshed
every 60s. Reads pending ``webhook_deliveries`` rows for its
subscription, builds the envelope, signs via ``signing.py``,
POSTs via ``http_client.py``, and updates the delivery row to
the terminal state (succeeded / failed / dlq). Per plan §2.3
the loop body holds the cursor advance + the body-equals-signed-
body runtime assertion at the HTTP-client boundary.
"""
