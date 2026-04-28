"""LISTEN/NOTIFY wake + fallback poll + Redis pubsub subscriber.

Filled in by D3. Three wake sources merge into one in-process
queue per plan §2.2:

  1. LISTEN on ``integration_events_visible`` (migration 031).
  2. 2-second fallback poll for missed NOTIFYs.
  3. Redis pubsub on ``webhook_subscription_events`` for cross-
     worker invalidation per the §2.9 action table.

D1 leaves this as a stub so the package layout is stable; the
daemon's main loop in ``__init__.py`` does not yet call into it.
"""
