"""Pre-accounting module app (#32 step 4).

A thin, separately-deployable FastAPI surface over the engine's existing
pre-accounting service layer (``saebooks.services.{quotes,purchase_orders,
time_entries}``). It runs the SAME service code as the engine, connected to
the SAME database (schema ``preaccounting``) with the SAME row-level-security
session pattern — the only difference is that it is packaged as its own
container/image so the pre-accounting deploy train is independent of the core
ledger, and the public web app never needs the pre-accounting code.

The module always runs with ``PREACCOUNTING_BASE_URL`` UNSET, so the engine
service functions execute IN-PROCESS here (no recursion back to itself). The
engine, when ``PREACCOUNTING_BASE_URL`` is set, delegates to this surface.
"""
