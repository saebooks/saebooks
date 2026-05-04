"""Non-versioned webhook routers.

Webhooks are not versioned by us — Stripe, Paperless, and other
providers POST to stable URLs we register in their dashboards once.
Putting them under ``/api/v1/`` would be misleading (Stripe doesn't
care about our API version) and would mean a future API v2 migration
would require updating all registered webhooks.

Routers in this package are mounted directly at ``/webhooks/`` in
``saebooks.main`` by explicit ``app.include_router`` calls.
"""
