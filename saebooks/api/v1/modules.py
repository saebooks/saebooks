"""``/api/v1/modules`` — the module-discovery / entitlement contract (M2).

Two endpoints, split across the auth boundary per
``m2-module-architecture-audit-2026-07-09.md`` §3 (forced by §8.2's
confirmed blocker: an unauthenticated route in this codebase never gets
``request.state.user`` -- ``ForwardAuthMiddleware`` explicitly skips
``/api/`` entirely, and only the per-route ``require_bearer`` dependency
stamps that state -- so an unauthenticated route structurally cannot
resolve a per-user effective edition):

* ``GET /api/v1/modules`` — UNAUTHENTICATED, static-only. Module id /
  label / kind / group / tier_membership / state, plus the static
  per-edition cap LIMITS matrix. Matches the existing ``/version``
  unauth-safe precedent, not ``/license``'s buggy singleton-only one.
  Never returns edition / effective_edition / entitled / health — none
  of those mean anything before a caller is authenticated.

* ``GET /api/v1/modules/usage`` — bearer-gated, tenant-scoped (M2 §5
  step 5). Per-user edition/effective_edition, per-module entitled +
  health, cap USAGE, bookkeeping_mode.

Both endpoints source their static shape from ``saebooks.services.
module_registry`` and therefore both automatically exclude the six
developer-only flags and the internal ``"developer"`` tier — the
registry never contains them (see that module's docstring).
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings
from saebooks.models.company import Company
from saebooks.services.companies import count_active_companies
from saebooks.services.features import _effective_edition_for_request, active_flags
from saebooks.services.licence import check_admin_seat, check_company, check_employee_seat
from saebooks.services.module_registry import (
    REGISTRY,
    ModuleEntry,
    caps_matrix,
    tier_membership_for,
)
from saebooks.services.users import count_admin_seats, count_employee_seats

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/modules", tags=["modules"])


def _static_module_dict(entry: ModuleEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "label": entry.label,
        "kind": entry.kind,
        "group": entry.group,
        "tier_membership": tier_membership_for(entry),
        "state": entry.state,
    }


@router.get("")
async def list_modules() -> dict[str, object]:
    """Unauthenticated, static-only module catalogue.

    Safe pre-login: every field here is edition-independent metadata
    (what modules exist, what tier they first turn on at, what the
    per-edition cap limits are) -- never per-user entitlement, never
    per-tenant health. See ``GET /api/v1/modules/usage`` for those.
    """
    return {
        "modules": [_static_module_dict(entry) for entry in REGISTRY],
        "caps": caps_matrix(),
    }


# ------------------------------------------------------------------ #
# GET /api/v1/modules/usage — bearer-gated, tenant-scoped (step 5)   #
# ------------------------------------------------------------------ #


async def _resolve_active_company_or_none(
    request: Request, x_company_id: str | None, session: AsyncSession
) -> Company | None:
    """Same resolution rule as ``deps.get_active_company_id`` (explicit
    ``X-Company-Id`` header, else the tenant's first active company),
    but returns ``None`` instead of raising 404 when nothing matches.

    A tenant between signup and first-company-creation (or one whose
    only company was just archived) is a legitimate, if brief, state —
    edition/entitlement/caps are still meaningful for such a caller,
    so ``/modules/usage`` must not hard-404 the WHOLE response just
    because company-scoped health/bookkeeping_mode has nothing to
    report. Company-scoped fields degrade to ``None``/``"ok"`` instead
    (see ``_module_usage_dict`` and the handler below).
    """
    tenant_id = resolve_tenant_id(request)
    if x_company_id:
        try:
            company_uuid = UUID(x_company_id)
        except ValueError:
            return None
        result = await session.execute(
            select(Company).where(
                Company.id == company_uuid,
                Company.tenant_id == tenant_id,
                Company.archived_at.is_(None),
            )
        )
        return result.scalars().first()
    result = await session.execute(
        select(Company)
        .where(Company.tenant_id == tenant_id, Company.archived_at.is_(None))
        .order_by(Company.created_at)
    )
    return result.scalars().first()


def _entitled_for(entry: ModuleEntry, flags_map: dict[str, bool]) -> bool:
    if entry.kind == "flag":
        assert entry.flag is not None
        return flags_map[entry.flag]
    if entry.kind == "delegated":
        # Union of the flags this delegated service wraps at the
        # caller's effective edition -- NOT hardcoded True. An empty
        # wrapped_flags tuple (platform/preaccounting) means the
        # module wraps no flag-gated capability, so it's
        # unconditionally entitled (see module_registry.py docstring
        # for why that's not the same leak as hardcoding capture's
        # entitled=True would be).
        if not entry.wrapped_flags:
            return True
        return any(flags_map[flag] for flag in entry.wrapped_flags)
    # kind == "mode" (cashbook) -- always entitled, no backing flag.
    return True


async def _health_for(
    entry: ModuleEntry, company: Company | None, session: AsyncSession
) -> str:
    """Per-module health, scoped to the caller's active company.

    Delegated modules default to ``"not_installed"`` -- M2 builds the
    delegated clients but does not activate live delegation containers
    (a separately-gated later decision), so there is genuinely nothing
    reachable to report health on yet. ``bank_feeds`` is the one module
    with a real tenant-scoped health signal today (the relay-down issue
    cache, keyed by ``BankFeedAccount.company_id`` -- migration 0016);
    everything else defaults to ``"ok"`` (M2 builds no other health
    source). Never raises: a health-check failure degrades to ``"ok"``
    rather than 500ing the whole usage response for an unrelated
    module.
    """
    if entry.kind == "delegated":
        return "not_installed"
    if entry.id == "bank_feeds" and company is not None:
        try:
            from saebooks.services.bank_feeds.health import active_issues_for_company

            issues = await active_issues_for_company(session, company.id)
            return "degraded" if issues else "ok"
        except Exception:
            _log.exception(
                "modules/usage: bank_feeds health check failed for company=%s "
                "-- defaulting to ok rather than failing the whole response",
                company.id,
            )
            return "ok"
    return "ok"


async def _module_usage_dict(
    entry: ModuleEntry,
    flags_map: dict[str, bool],
    company: Company | None,
    session: AsyncSession,
) -> dict[str, object]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "entitled": _entitled_for(entry, flags_map),
        "health": await _health_for(entry, company, session),
    }


@router.get("/usage", dependencies=[Depends(require_bearer)])
async def modules_usage(
    request: Request,
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Bearer-gated, tenant-scoped module usage / entitlement.

    ``require_bearer`` stamps ``request.state.user`` (see
    ``saebooks/api/v1/auth.py``), which is the prerequisite M2 §5 step 1
    closes: ``_effective_edition_for_request`` can only resolve a
    per-user launch-promo-JWT override on a route that carries this
    dependency, which this route does (unlike the unauthenticated
    ``GET /api/v1/modules``).

    Response shape::

        {
          "edition": "community",             # process-wide singleton
          "effective_edition": "pro",         # per-request resolved
          "bookkeeping_mode": "full" | "cashbook" | null,
          "modules": [
            {"id": ..., "kind": ..., "entitled": bool, "health": str},
            ...
          ],
          "caps": {
            "admin_seats": {"outcome", "limit", "current", "reason"},
            "employee_seats": {...},
            "companies": {...},
          }
        }

    RLS: every query runs through ``get_session``'s tenant-bound
    session (``app.current_tenant`` set per request), and the active
    company is additionally scoped to ``Company.tenant_id`` -- a
    caller can never resolve, and therefore never see cap usage or
    bank-feeds health for, another tenant's company via
    ``X-Company-Id``.
    """
    effective_edition = _effective_edition_for_request(request)
    flags_map = active_flags(edition=effective_edition)

    company = await _resolve_active_company_or_none(request, x_company_id, session)

    modules = [
        await _module_usage_dict(entry, flags_map, company, session)
        for entry in REGISTRY
    ]

    admin_count = await count_admin_seats(session)
    employee_count = await count_employee_seats(session)
    company_count = await count_active_companies(session)

    return {
        "edition": settings.edition,
        "effective_edition": effective_edition,
        "bookkeeping_mode": company.bookkeeping_mode if company is not None else None,
        "modules": modules,
        "caps": {
            "admin_seats": asdict(check_admin_seat(effective_edition, admin_count)),
            "employee_seats": asdict(
                check_employee_seat(effective_edition, employee_count)
            ),
            "companies": asdict(check_company(effective_edition, company_count)),
        },
    }
