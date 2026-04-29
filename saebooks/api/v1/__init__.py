"""API v1 — pure JSON routers for the self-host API-first rebuild.

Phase 0 scope: ``/api/v1/contacts``, ``/api/v1/changes``,
``/api/v1/snapshot``.
Phase 1 tier-1: ``/api/v1/accounts``, ``/api/v1/companies``,
``/api/v1/tax_codes``.
Phase 1 tier-2: ``/api/v1/users``, ``/api/v1/permissions``,
``/api/v1/items``.
Phase 1 tier-3: ``/api/v1/journal_entries``, ``/api/v1/invoices``,
``/api/v1/bills``, ``/api/v1/payments``, ``/api/v1/credit_notes``.
Phase 1 tier-4: ``/api/v1/bank_accounts``,
``/api/v1/bank_statement_lines``, ``/api/v1/projects``,
``/api/v1/fixed_assets``, ``/api/v1/recurring_invoices``,
``/api/v1/budgets``.
Phase 1 tier-5: ``/api/v1/reports/aged_receivables``,
``/api/v1/reports/aged_payables``.
B/46: ``/api/v1/documents/extract`` (AI document extraction).
0077: ``/api/v1/auth/signup``, verify-email, password-reset, magic-link.
0078: ``/api/v1/billing/checkout-session``, ``/api/v1/billing/webhook``.
"""
from fastapi import APIRouter

from saebooks.api.v1.allocations import router as allocations_router
from saebooks.api.v1.account_ranges import router as account_ranges_router
from saebooks.api.v1.ai_extraction import router as ai_extraction_router
from saebooks.api.v1.accounts import router as accounts_router
from saebooks.api.v1.bank_accounts import router as bank_accounts_router
from saebooks.api.v1.bank_rules import router as bank_rules_router
from saebooks.api.v1.billing import router as billing_router
from saebooks.api.v1.depreciation_models import router as depreciation_models_router
from saebooks.api.v1.bank_statement_lines import router as bank_statement_lines_router
from saebooks.api.v1.bills import router as bills_router
from saebooks.api.v1.budgets import router as budgets_router
from saebooks.api.v1.changes import router as changes_router
from saebooks.api.v1.companies import router as companies_router
from saebooks.api.v1.contacts import router as contacts_router
from saebooks.api.v1.credit_notes import router as credit_notes_router
from saebooks.api.v1.fixed_assets import router as fixed_assets_router
from saebooks.api.v1.health import router as health_router
from saebooks.api.v1.login import router as login_router
from saebooks.api.v1.reconciliation import router as reconciliation_router
from saebooks.api.v1.recurring_invoices import router as recurring_invoices_router
from saebooks.api.v1.reports import router as reports_router
from saebooks.api.v1.search import router as search_router
from saebooks.api.v1.signup import router as signup_router
from saebooks.api.v1.invoices import router as invoices_router
from saebooks.api.v1.journal_templates import router as journal_templates_router
from saebooks.api.v1.items import router as items_router
from saebooks.api.v1.journal_entries import router as journal_entries_router
from saebooks.api.v1.payments import router as payments_router
from saebooks.api.v1.projects import router as projects_router
from saebooks.api.v1.snapshot import router as snapshot_router
from saebooks.api.v1.tax_codes import router as tax_codes_router
from saebooks.api.v1.users import permissions_router, router as users_router

# One umbrella router — main.py mounts this at /api/v1.
router = APIRouter(prefix="/api/v1")
# health first — /api/v1/healthz + /api/v1/version are deliberately
# unauthenticated (no require_bearer), so they must mount before any
# router with a router-level dependency.
router.include_router(health_router)
# JWT login endpoints — unauthenticated entry points; must come before
# any router with a router-level bearer dependency so /auth/login etc.
# are never gated by require_bearer.
router.include_router(login_router)
# Public signup / verify / reset / magic-link — also unauthenticated.
# Mounted right after login so /auth/signup, /auth/verify-email etc.
# share the same gate-free prefix.
router.include_router(signup_router)
# Stripe billing — /billing/checkout-session is auth-gated by its own
# explicit dependency; /billing/webhook is unauthenticated (Stripe
# auth is by signature, not bearer). The router itself isn't gated.
router.include_router(billing_router)
router.include_router(contacts_router)
router.include_router(account_ranges_router)
router.include_router(accounts_router)
router.include_router(bank_accounts_router)
router.include_router(bank_rules_router)
router.include_router(bank_statement_lines_router)
router.include_router(budgets_router)
router.include_router(companies_router)
router.include_router(tax_codes_router)
router.include_router(items_router)
router.include_router(users_router)
router.include_router(permissions_router)
router.include_router(journal_entries_router)
router.include_router(invoices_router)
router.include_router(journal_templates_router)
router.include_router(bills_router)
router.include_router(payments_router)
router.include_router(credit_notes_router)
router.include_router(projects_router)
router.include_router(fixed_assets_router)
router.include_router(depreciation_models_router)
router.include_router(reconciliation_router)
router.include_router(recurring_invoices_router)
router.include_router(reports_router)
router.include_router(search_router)
router.include_router(changes_router)
router.include_router(snapshot_router)
# B/46: AI document extraction — feature-gated to Business+ via
# FLAG_AI_EXTRACTION. Mounted last to stay after the auth/login routers.
router.include_router(ai_extraction_router)
# FITC-6: allocation rules engine — Business+ feature-gated
router.include_router(allocations_router)

__all__ = ["router"]
