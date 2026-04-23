"""API v1 — pure JSON routers for the self-host API-first rebuild.

Phase 0 scope: ``/api/v1/contacts``, ``/api/v1/changes``,
``/api/v1/snapshot``.
Phase 1 tier-1: ``/api/v1/accounts``, ``/api/v1/companies``,
``/api/v1/tax_codes``.
Phase 1 tier-2: ``/api/v1/users``, ``/api/v1/permissions``,
``/api/v1/items``.
Phase 1 tier-3: ``/api/v1/journal_entries``, ``/api/v1/invoices``,
``/api/v1/bills``, ``/api/v1/payments``, ``/api/v1/credit_notes``.
Phase 1 tier-4 will follow: bank_accounts, bank_statement_lines, etc.
"""
from fastapi import APIRouter

from saebooks.api.v1.accounts import router as accounts_router
from saebooks.api.v1.bills import router as bills_router
from saebooks.api.v1.changes import router as changes_router
from saebooks.api.v1.companies import router as companies_router
from saebooks.api.v1.contacts import router as contacts_router
from saebooks.api.v1.credit_notes import router as credit_notes_router
from saebooks.api.v1.invoices import router as invoices_router
from saebooks.api.v1.items import router as items_router
from saebooks.api.v1.journal_entries import router as journal_entries_router
from saebooks.api.v1.payments import router as payments_router
from saebooks.api.v1.snapshot import router as snapshot_router
from saebooks.api.v1.tax_codes import router as tax_codes_router
from saebooks.api.v1.users import permissions_router, router as users_router

# One umbrella router — main.py mounts this at /api/v1.
router = APIRouter(prefix="/api/v1")
router.include_router(contacts_router)
router.include_router(accounts_router)
router.include_router(companies_router)
router.include_router(tax_codes_router)
router.include_router(items_router)
router.include_router(users_router)
router.include_router(permissions_router)
router.include_router(journal_entries_router)
router.include_router(invoices_router)
router.include_router(bills_router)
router.include_router(payments_router)
router.include_router(credit_notes_router)
router.include_router(changes_router)
router.include_router(snapshot_router)

__all__ = ["router"]
