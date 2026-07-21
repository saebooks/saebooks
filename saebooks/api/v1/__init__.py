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
0080: ``/api/v1/contact/submit`` — public contact form.

M2 §5 build-sequence step 2 (module-architecture audit
2026-07-09, "Layer A — Engine import + mount isolation"): this module
used to import every router at the top of the file (~85 static
``from saebooks.api.v1.<module> import router as ...`` statements) and
``include_router()`` each one unconditionally. Because FastAPI route
registration runs via ``@router.get(...)`` decorators AT IMPORT TIME,
a single broken router module (bad ``response_model``, duplicate
``operation_id``, a broken transitive import) killed the *entire*
``__init__.py`` import and therefore the whole app's boot — no
try/except around ``include_router()`` could ever catch it, because
the failure happens before that code even runs.

Replaced with a manifest-driven guarded-import loop
(``build_v1_router`` / ``MODULE_MANIFEST`` below): each router is
``importlib.import_module()``-ed individually, inside try/except that
catches both ``ImportError`` and any exception raised during
decorator-time route registration. On failure, a stub sub-router is
mounted at that entry's prefix returning ``503
{"module": ..., "status": "unavailable"}`` — instead of failing app
boot — wherever it is *safe* to do so (see ``_stub_safe_prefixes``).
On success, the real router is ``include_router()``-ed exactly as
before: same object, same call, same order. The success path is
byte-for-byte unchanged from the prior static-import version.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RouterSpec:
    """One entry in the guarded-import manifest.

    ``module`` — dotted name under ``saebooks.api.v1`` (e.g.
    ``"contacts"``) to ``importlib.import_module()``.

    ``attr`` — name of the ``APIRouter`` object inside that module.
    ``"router"`` for the overwhelming majority of modules; four modules
    export a SECOND named router mounted at its own manifest position:
    ``license._promo_router``, ``principal_auth.auth_router`` (plus its
    ``router``), ``users.permissions_router``, and
    ``integrations.public_router``. Each gets its own manifest entry —
    a naive one-router-per-module loop would silently drop these.

    ``prefix`` — the router's OWN mount prefix, duplicated here rather
    than introspected from the router object post-import, so:

    1. A *failed* import still knows where its 503 stub belongs (you
       cannot read ``.prefix`` off an object that never imported).
    2. The safe-to-stub computation (``_stub_safe_prefixes``) can run
       once, statically, before any module is imported.
    """

    module: str
    attr: str
    prefix: str


# Reproduces the pre-M2 include_router() order verbatim (see git history
# for saebooks/api/v1/__init__.py prior to this commit) — order is not
# known to matter functionally (every router owns its own prefix and its
# own dependencies), but preserving it is free insurance against a
# latent ordering assumption somewhere downstream, so don't reorder
# without a reason.
MODULE_MANIFEST: tuple[_RouterSpec, ...] = (
    # --- Kernel: health/version + unauthenticated entry points --------
    # Mounted first, exactly as before. These stay in the SAME guarded
    # loop as everything else (a broken health.py shouldn't be able to
    # crash boot either), but several of them share an exact prefix
    # with a sibling (login+signup both "/auth"; principal_auth's two
    # routers both "/principal"; license's two routers both "/license")
    # so they are never *stub*-eligible — see _stub_safe_prefixes.
    _RouterSpec("health", "router", ""),
    _RouterSpec("login", "router", "/auth"),
    _RouterSpec("principal_auth", "auth_router", "/principal"),
    _RouterSpec("signup", "router", "/auth"),
    _RouterSpec("billing", "router", "/billing"),
    _RouterSpec("license", "router", "/license"),
    _RouterSpec("license", "_promo_router", "/license"),
    _RouterSpec("lodgement", "router", "/lodgement"),
    _RouterSpec("contact_public", "router", "/contact"),
    # M2 module registry (§5 steps 3+5): GET /modules (unauthenticated,
    # static) + GET /modules/usage (bearer-gated, tenant-scoped). See
    # saebooks/services/module_registry.py + saebooks/api/v1/modules.py.
    _RouterSpec("modules", "router", "/modules"),
    # --- Business-domain routers ---------------------------------------
    _RouterSpec("contacts", "router", "/contacts"),
    _RouterSpec("one_off_vendors", "router", "/one-off-vendors"),
    _RouterSpec("one_off_customers", "router", "/one-off-customers"),
    _RouterSpec("account_ranges", "router", "/account_ranges"),
    _RouterSpec("accounts", "router", "/accounts"),
    _RouterSpec("ato_sbr", "router", "/ato_sbr"),
    _RouterSpec("bank_accounts", "router", "/bank_accounts"),
    # Cat-C (W4): bank-feeds relay client — Business+ feature-gated.
    _RouterSpec("bank_feeds", "router", "/bank-feeds"),
    _RouterSpec("bank_rules", "router", "/bank_rules"),
    _RouterSpec("bank_statement_lines", "router", "/bank_statement_lines"),
    _RouterSpec("budgets", "router", "/budgets"),
    _RouterSpec("companies", "router", "/companies"),
    _RouterSpec("tax_codes", "router", "/tax_codes"),
    _RouterSpec("items", "router", "/items"),
    _RouterSpec("users", "router", "/users"),
    _RouterSpec("users", "permissions_router", "/permissions"),
    # granular_permissions module (D2): tenant-scoped custom roles,
    # FLAG_GRANULAR_PERMISSIONS-gated (Offline+). See services/roles.py.
    _RouterSpec("roles", "router", "/roles"),
    # Wave B: theme catalogue, FLAG_THEMES-gated. See services/theme.py.
    _RouterSpec("themes", "router", "/themes"),
    _RouterSpec("journal_entries", "router", "/journal_entries"),
    _RouterSpec("invoices", "router", "/invoices"),
    _RouterSpec("journal_templates", "router", "/journal_templates"),
    _RouterSpec("bills", "router", "/bills"),
    _RouterSpec("branches", "router", "/branches"),
    _RouterSpec("expenses", "router", "/expenses"),
    _RouterSpec("time_entries", "router", "/time-entries"),
    # Payroll Phase 1A foundations
    _RouterSpec("super_funds", "router", "/super-funds"),
    _RouterSpec("employees", "router", "/employees"),
    # Payroll Phase 3 — STP Phase 2 submission storage (no live submit yet)
    _RouterSpec("stp", "router", "/stp-submissions"),
    # Payroll Phase 4 — leave balances + adjust
    _RouterSpec("leave", "router", "/leave"),
    _RouterSpec("purchase_orders", "router", "/purchase_orders"),
    _RouterSpec("quotes", "router", "/quotes"),
    _RouterSpec("email_log", "router", "/email-log"),
    # Resend webhook — unauthenticated (signature-verified instead) so it
    # mounts alongside the other webhook receivers.
    _RouterSpec("webhooks_resend", "router", "/webhooks/resend"),
    _RouterSpec("payments", "router", "/payments"),
    # DB-rebuild handover #2: first-class Transfer (account-to-account
    # money movement) record type. See saebooks/services/transfers.py.
    _RouterSpec("transfers", "router", "/transfers"),
    # Intercompany Phase 1 (LOCAL / same-tenant) record type.
    _RouterSpec("intercompany", "router", "/intercompany"),
    # Public inbound relay webhook (per-edge token + Ed25519, no JWT) — the
    # receiver half of the cross-DB intercompany relay (Phase 3c); gated
    # default-off by SAEBOOKS_IC_REMOTE_RELAY_ENABLED (returns 503 when off).
    # Shares "/intercompany" exactly with the entry above.
    _RouterSpec("intercompany", "public_router", "/intercompany"),
    # Gap 2 (0158): first-class Reclassification.
    # See saebooks/services/reclassifications.py.
    _RouterSpec("reclassifications", "router", "/reclassifications"),
    _RouterSpec("credit_notes", "router", "/credit_notes"),
    # 0157 money-in record types: supplier (purchase) credit note +
    # generic money-in receipt.
    _RouterSpec("supplier_credit_notes", "router", "/supplier_credit_notes"),
    _RouterSpec("receipts", "router", "/receipts"),
    _RouterSpec("projects", "router", "/projects"),
    _RouterSpec("fixed_assets", "router", "/fixed_assets"),
    _RouterSpec("depreciation_models", "router", "/depreciation_models"),
    _RouterSpec("document_inbox", "router", "/inbox"),
    _RouterSpec("reconciliation", "router", "/reconciliation"),
    _RouterSpec("recurring_invoices", "router", "/recurring_invoices"),
    _RouterSpec("reports", "router", "/reports"),
    _RouterSpec("period_close", "router", "/period-close"),
    # /api/v1/proration — pure-math prorate previews + deferred-revenue
    # recognise. See saebooks/services/proration.py.
    _RouterSpec("proration", "router", "/proration"),
    _RouterSpec("search", "router", "/search"),
    _RouterSpec("changes", "router", "/changes"),
    _RouterSpec("snapshot", "router", "/snapshot"),
    # B/46: AI document extraction — feature-gated to Business+ via
    # FLAG_AI_EXTRACTION.
    _RouterSpec("ai_extraction", "router", "/documents"),
    # FITC-6: allocation rules engine — Business+ feature-gated
    _RouterSpec("allocations", "router", "/allocation_rules"),
    # Phase 1 vault wire-in: /api/v1/attachments — proxies blobs to
    # saebooks-vault. Returns 503 when vault_enabled=false.
    _RouterSpec("attachments", "router", "/attachments"),
    # Cat-C (W5): admin audit-log + SQL tool (FLAG_SQL_TOOL Pro+).
    _RouterSpec("admin", "router", "/admin"),
    _RouterSpec("audit_log", "router", "/audit-log"),
    # FLAG_RAW_JSON_INSPECTOR — developer-tier-only raw-row + change_log
    # debug endpoint. NOTE: "/admin/inspect" is a CHILD of "/admin"'s
    # prefix, so neither "/admin" nor this entry's stub can safely
    # catch-all if "/admin" itself is the one that fails — see
    # _stub_safe_prefixes.
    _RouterSpec("admin_inspect", "router", "/admin/inspect"),
    # FLAG_TENANT_SWITCHER — list tenants on the instance for the switcher.
    _RouterSpec("admin_tenants", "router", "/admin/tenants"),
    _RouterSpec("api_tokens", "router", "/api-tokens"),
    # Wave E: per-tenant scheduled backups (FLAG_SCHEDULED_BACKUPS Pro+).
    # See services/scheduled_backups.py + services/backup_export.py.
    _RouterSpec("scheduled_backups", "router", "/scheduled-backups"),
    # Cat-C: multi-step import wizard (bank CSV/OFX community; QBO Pro+)
    _RouterSpec("imports", "router", "/imports"),
    # Cat-C (W6): integrations -- Stripe Connect, Paperless, LEI, CH, ATO.
    _RouterSpec("integrations", "router", "/integrations"),
    # Public webhook routes (HMAC-authenticated, no JWT required) — shares
    # "/integrations" exactly with the entry above.
    _RouterSpec("integrations", "public_router", "/integrations"),
    # Cat-C (W1): pay-run / payroll v1 endpoints.
    _RouterSpec("pay_run", "router", "/pay-runs"),
    # Cashbook edition (single-entry UI over double-entry storage) — see
    # docs/cashbook-edition-design.md and saebooks.services.cashbook.
    _RouterSpec("cashbook", "router", "/cashbook"),
    # WebAuthn / FIDO2 — native passkey support at the app layer.
    _RouterSpec("webauthn", "router", "/auth/webauthn"),
    # Authenticated principal endpoints (register key / list tenants /
    # act-as / bound reads). REVIEW BRANCH feat/accountant-login — cross-
    # tenant surface, not deployed.
    _RouterSpec("principal_auth", "router", "/principal"),
    _RouterSpec("principal_grants", "router", "/principal-grants"),
    _RouterSpec("tpar", "router", "/tpar"),
    # Payday Super Phase 1 — SAFF generation + lodgement tracking
    _RouterSpec("super_lodgements", "router", "/super_lodgements"),
    _RouterSpec("tax_returns", "router", "/tax_returns"),
    # Gitea #28: supplier-statement reconciliation queue (Phase 1)
    _RouterSpec("statements", "router", "/statements"),
    # Gitea #28 P4: supplier-statement extraction-hint templates
    _RouterSpec("statement_templates", "router", "/statement-templates"),
    # Accounting-package sync — Enterprise tier, FLAG_ACCOUNTING_SYNC +
    # FLAG_SYNC_XERO gated. See saebooks/services/sync/xero/ + docs/sync/xero.md.
    _RouterSpec("sync_xero", "router", "/sync/xero"),
)


def _stub_safe_prefixes(manifest: tuple[_RouterSpec, ...]) -> frozenset[str]:
    """Return the subset of ``manifest`` prefixes safe to 503-stub.

    A prefix is UNSAFE to mount a catch-all stub at when:

    * it's empty (``""``) — a wildcard there would swallow the entire
      unmatched ``/api/v1`` namespace, not just one module;
    * another manifest entry shares the exact same prefix — stubbing
      one sibling would either shadow the other (if mounted first) or
      be shadowed by it (if mounted second); neither is correct, so
      neither sibling gets a catch-all — an unmatched path under that
      prefix falls through to a normal 404 instead;
    * another manifest entry's prefix is a CHILD of this one (starts
      with ``this_prefix + "/"``) — a wildcard mounted at the parent's
      position in the route list would intercept the child's real
      routes too, because Starlette matches routes in registration
      order, not by specificity. ``/admin`` is a parent of
      ``/admin/inspect`` and ``/admin/tenants`` for exactly this
      reason.

    Computed once from the manifest's DECLARED prefixes (a plain
    string comparison), so it's available even for an entry whose
    import just failed and therefore never produced a router object to
    introspect.
    """
    all_prefixes = [spec.prefix for spec in manifest]
    safe: set[str] = set()
    for spec in manifest:
        prefix = spec.prefix
        if not prefix:
            continue
        if all_prefixes.count(prefix) > 1:
            continue
        if any(
            other != prefix and other.startswith(prefix + "/")
            for other in all_prefixes
        ):
            continue
        safe.add(prefix)
    return frozenset(safe)


def _stub_router(prefix: str, module_id: str) -> APIRouter:
    """Build a sub-router that answers every path under ``prefix`` with
    ``503 {"module": module_id, "status": "unavailable"}``.

    Mounted in place of a router whose import failed, so the rest of
    the app still boots. Covers both the bare prefix (``GET /billing``)
    and any nested path (``GET /billing/checkout-session``).
    """
    stub = APIRouter(prefix=prefix)

    async def _unavailable(_rest_of_path: str = "") -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"module": module_id, "status": "unavailable"},
        )

    _methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    stub.add_api_route(
        "",
        _unavailable,
        methods=_methods,
        include_in_schema=False,
    )
    stub.add_api_route(
        "/{_rest_of_path:path}",
        _unavailable,
        methods=_methods,
        include_in_schema=False,
    )
    return stub


def build_v1_router(manifest: tuple[_RouterSpec, ...] = MODULE_MANIFEST) -> APIRouter:
    """Assemble the ``/api/v1`` umbrella router from ``manifest``.

    Each entry is imported INSIDE try/except, catching both
    ``ImportError`` and any exception raised while the module body
    executes (including decorator-time route-registration errors — a
    bad ``response_model``, a duplicate ``operation_id``, etc.). On
    failure, a 503 stub is mounted at that entry's prefix when doing so
    is safe (see ``_stub_safe_prefixes``); otherwise the failure is
    logged and nothing is mounted for that entry (real sibling routes
    under a shared/parent prefix are left alone, and unmatched paths
    fall through to the app's normal 404).

    On success, the real router object is ``include_router()``-ed
    exactly as the prior static-import version did — same object, same
    call, same order. The success path is unchanged.
    """
    root = APIRouter(prefix="/api/v1")
    stub_safe = _stub_safe_prefixes(manifest)
    imported_modules: dict[str, Any] = {}

    for spec in manifest:
        module_id = f"{spec.module}.{spec.attr}"
        try:
            module = imported_modules.get(spec.module)
            if module is None:
                module = importlib.import_module(f"saebooks.api.v1.{spec.module}")
                imported_modules[spec.module] = module
            router_obj = getattr(module, spec.attr)
        except Exception:
            if spec.prefix in stub_safe:
                _log.exception(
                    "guarded import failed for %s (prefix=%r) -- "
                    "mounting unavailable stub",
                    module_id,
                    spec.prefix,
                )
                root.include_router(_stub_router(spec.prefix, module_id))
            else:
                _log.exception(
                    "guarded import failed for %s (prefix=%r) -- no "
                    "safe catch-all prefix (shared or parent prefix); "
                    "real routes under this prefix will 404, not 503",
                    module_id,
                    spec.prefix,
                )
            continue
        root.include_router(router_obj)

    return root


# One umbrella router — main.py mounts this at /api/v1.
router = build_v1_router()

__all__ = ["MODULE_MANIFEST", "_RouterSpec", "build_v1_router", "router"]
