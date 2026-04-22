"""API v1 — pure JSON routers for the self-host API-first rebuild.

Phase 0 scope: ``/api/v1/contacts``, ``/api/v1/changes``,
``/api/v1/snapshot``. Phase 1 will extend to the full entity set
(invoices, bills, accounts, journal, bank feeds, …).
"""
from fastapi import APIRouter

from saebooks.api.v1.changes import router as changes_router
from saebooks.api.v1.contacts import router as contacts_router
from saebooks.api.v1.snapshot import router as snapshot_router

# One umbrella router — main.py mounts this at /api/v1.
router = APIRouter(prefix="/api/v1")
router.include_router(contacts_router)
router.include_router(changes_router)
router.include_router(snapshot_router)

__all__ = ["router"]
