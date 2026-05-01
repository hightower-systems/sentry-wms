"""Per-subscription requests.Session lifecycle.

Filled in by D9 + D11. Each subscription's HTTP session is reused
across deliveries to amortize TLS handshake cost. The DNS-rebinding
mitigation invariant (plan §2.7) requires that a subscription
mutation that changes the resolved network destination MUST force
DNS resolution to re-occur on the next dispatch. The simplest
mechanism is to tear down the session on any subscription-level
event that could change DNS (delivery_url_changed, paused, deleted,
secret_rotated when paired with a URL change), per the §2.9 action
table.
"""
