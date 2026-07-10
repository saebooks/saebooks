"""Capture module app (#32 step 5).

A separately-deployable surface over the engine's existing capture code —
the imports wizard (``api/v1/imports``), the bank-feeds REST surface
(``api/v1/bank_feeds``) and AI document extraction
(``services/ai_extraction``) — plus a background worker that runs the
``sync-feeds`` / ``reconcile-feeds`` CLI jobs on an interval.

It runs the SAME code as the engine, connected to the SAME database (the
capture-owned tables live in schema ``capture``; ``search_path`` keeps the
ORM resolving them) with the SAME row-level-security session pattern — the
only difference is that it is packaged as its own container/image so the
capture deploy train (and its SISS / relay / LiteLLM dependencies) is
independent of the core ledger, and the public web app never needs the
capture code.

The module always runs with ``CAPTURE_BASE_URL`` UNSET, so the engine code
executes IN-PROCESS here (no recursion back to itself). The engine, when
``CAPTURE_BASE_URL`` is set, delegates to this surface.

Two entrypoints, one image (selected by the ``MODE`` env var, dispatched in
``capture_app.__main__``):

* ``MODE=web``    — ``uvicorn capture_app.main:app`` (default)
* ``MODE=worker`` — ``capture_app.worker`` asyncio loop
"""
