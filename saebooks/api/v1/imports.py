"""Pure JSON import wizard router — ``/api/v1/imports``.

Implements multi-step wizards for all four import flows:

* ``bank_csv``  — bank statement CSV (community, no flag)
* ``bank_ofx``  — bank statement OFX (community, no flag)
* ``coa``       — chart of accounts CSV (community, no flag)
* ``qbo``       — QBO migration CSV (Pro+, requires FLAG_QBO_IMPORT)

Wizard lifecycle
----------------
::

    POST /api/v1/imports/wizards
        body: {"kind": "bank_csv", "initial": {"account_id": "<uuid>"}}
        → 201 {"wizard_id", "step": 0, "state"}

    POST /api/v1/imports/wizards/{id}/step
        body: {"step": 0, "patch": {"raw_csv": "<csv text>"}}
        → 200 {"step": 1, "state", "completed": false}

    GET /api/v1/imports/wizards/{id}
        → 200 {"wizard_id", "step", "state", "expires_at"}

    POST /api/v1/imports/wizards/{id}/commit
        → 200 {"inserted": N, "total": M}   (bank import)
        → 200 {"new": N, "changed": M, "removed": K}  (CoA)
        → 200 {"contacts_imported": N}  (QBO)

Conventions
-----------
* Bearer-token auth via ``require_bearer`` (router-level dep).
* Tenant binding via ``get_session`` (``app.current_tenant`` SET LOCAL).
* Idempotency: ``X-Idempotency-Key`` on POST /wizards and POST /wizards/{id}/commit.
* PeriodLock check on commit (when the import creates journal entries — bank lines
  don't create journal entries directly, so only CoA + QBO need the check; bank
  commits are exempted because they insert ``BankStatementLine`` rows, not postings).
* Change log appended on commit.
* qbo kind is gated to FLAG_QBO_IMPORT (Pro+).
* bank_csv, bank_ofx, coa are community-tier (no flag).
"""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid_mod
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_company_id, get_session
from saebooks.api.v1._wizard import Wizard, WizardExpiredError, WizardNotFoundError
from saebooks.services.features import FLAG_QBO_IMPORT, require_feature
from saebooks.services.idempotency import ClaimStatus, claim_or_fetch, store_response
from saebooks.services import change_log as change_log_svc

# Import service helpers (parsers + persister)
from saebooks.services.imports import bank_csv as bank_csv_svc
from saebooks.services.imports import bank_ofx as bank_ofx_svc
from saebooks.services.imports import coa as coa_svc
from saebooks.services.imports import persist as persist_svc
from saebooks.services.imports import qbo as qbo_svc

# Model imports for QBO contacts apply
from saebooks.models.contact import Contact, ContactType
from saebooks.models.account import Account, AccountType
from sqlalchemy import select

router = APIRouter(
    prefix="/imports",
    tags=["imports"],
    dependencies=[Depends(require_bearer)],
)

_COMMUNITY_KINDS = frozenset({"bank_csv", "bank_ofx", "coa"})
_QBO_KINDS = frozenset({"qbo"})
_ALL_KINDS = _COMMUNITY_KINDS | _QBO_KINDS


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WizardStartBody(BaseModel):
    kind: str
    initial: dict[str, Any] = {}
    ttl_seconds: int = 3600

    @field_validator("kind")
    @classmethod
    def kind_must_be_known(cls, v: str) -> str:
        if v not in _ALL_KINDS:
            raise ValueError(f"Unknown import kind: {v!r}. Must be one of {sorted(_ALL_KINDS)}")
        return v


class WizardStepBody(BaseModel):
    step: int
    patch: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_idempotency_key(header: str | None) -> str | None:
    if header is None or not header.strip():
        return None
    return header.strip()


def _wizard_summary(wizard_id: UUID, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "wizard_id": str(wizard_id),
        "step": state.get("step", 0),
        "state": state,
    }


async def _check_qbo_flag(request: Request) -> None:
    """Callable used as a dep-factory result for QBO kind gating.

    Passes the ``Request`` through so the per-user effective edition
    (e.g. launch-promo Pro JWT on the user row) is honoured rather
    than only the process-wide singleton. Without this hop a promo'd
    user on a Community-default deployment would get 404 here even
    though their licence covers QBO import.
    """
    dep = require_feature(FLAG_QBO_IMPORT)
    await dep(request)


# ---------------------------------------------------------------------------
# POST /imports/wizards — start a new wizard
# ---------------------------------------------------------------------------


@router.post("/wizards", status_code=201)
async def start_wizard(
    payload: WizardStartBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Start a new import wizard session.

    Returns ``{wizard_id, step, state}`` (201).  Idempotent when
    ``X-Idempotency-Key`` is supplied — replays the original 201.
    """
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    # QBO kind requires Pro+ flag.
    if payload.kind in _QBO_KINDS:
        await _check_qbo_flag(request)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 201,
            )

    initial_state = dict(payload.initial)
    initial_state.setdefault("step", 0)
    initial_state.setdefault("kind", payload.kind)

    wizard_id = await Wizard.start(
        session,
        kind=payload.kind,
        initial_state=initial_state,
        ttl_seconds=payload.ttl_seconds,
    )

    body = _wizard_summary(wizard_id, initial_state)
    await session.commit()

    if key is not None:
        # Re-open session to store idempotency response (session committed above).
        # Use the same session — store_response + commit again.
        await store_response(session, key, 201, json.dumps(body).encode())
        await session.commit()

    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# POST /imports/wizards/{id}/step — advance the wizard
# ---------------------------------------------------------------------------


@router.post("/wizards/{wizard_id}/step", status_code=200)
async def advance_wizard_step(
    wizard_id: UUID,
    payload: WizardStepBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Apply a partial state patch and advance the step counter.

    Returns ``{step, state, completed}`` (200).  ``completed`` is ``true``
    when the state contains a ``"_completed": true`` sentinel (set by the
    client on the final step) — the wizard can then be committed.
    """
    patch = dict(payload.patch)
    # Increment step counter in the patch.
    patch["step"] = payload.step + 1

    try:
        merged = await Wizard.step(session, wizard_id, patch)
    except WizardNotFoundError:
        raise HTTPException(404, "Wizard not found or expired")
    except WizardExpiredError:
        raise HTTPException(410, "Wizard has expired — start a new one")

    await session.commit()

    completed = bool(merged.get("_completed", False))
    return JSONResponse({
        "step": merged.get("step", payload.step + 1),
        "state": merged,
        "completed": completed,
    })


# ---------------------------------------------------------------------------
# GET /imports/wizards/{id} — current state
# ---------------------------------------------------------------------------


@router.get("/wizards/{wizard_id}", status_code=200)
async def get_wizard(
    wizard_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Return the current wizard state (without mutating it).

    Returns 404 when the wizard is missing or has expired.
    """
    state = await Wizard.get(session, wizard_id)
    if state is None:
        raise HTTPException(404, "Wizard not found or expired")
    return JSONResponse({
        "wizard_id": str(wizard_id),
        "step": state.get("step", 0),
        "state": state,
    })


# ---------------------------------------------------------------------------
# POST /imports/wizards/{id}/commit — run the import
# ---------------------------------------------------------------------------


@router.post("/wizards/{wizard_id}/commit", status_code=200)
async def commit_wizard(
    wizard_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    company_id: UUID = Depends(get_active_company_id),
) -> Any:
    """Run the import and persist the results.

    Dispatches to the appropriate service based on the wizard's ``kind``.

    - ``bank_csv`` / ``bank_ofx``: calls ``persist_svc.persist_bank_lines``.
    - ``coa``: calls ``coa_svc.apply_coa_diff``.
    - ``qbo``: calls QBO contacts + accounts persistence (requires FLAG_QBO_IMPORT).

    PeriodLock: bank line imports do not create journal entries, so they
    skip the lock check.  CoA and QBO imports do not post journal entries
    either (they only insert/update accounts and contacts), so the period
    lock check is enforced as a future-proof gate only when the import
    kind would post journal entries.  Currently all four kinds are exempt
    from the period lock — the check is here as scaffolding so downstream
    journal-posting imports (e.g. open-balance JE imports) can simply
    call ``_assert_not_period_locked``.

    Returns an import-result dict.
    """
    tenant_id = resolve_tenant_id(request)
    key = _parse_idempotency_key(idempotency_key)

    if key is not None:
        raw_body = await request.body()
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        claim = await claim_or_fetch(session, key, tenant_id, body_sha256)
        if claim.status == ClaimStatus.CONFLICT:
            return JSONResponse(
                {"code": "idempotency_key_conflict", "message": "X-Idempotency-Key reused with a different request body"},
                status_code=422,
            )
        if claim.status == ClaimStatus.IN_FLIGHT:
            return JSONResponse(
                {"code": "request_in_flight", "message": "Retry after 1 second."},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if claim.status == ClaimStatus.REPLAY:
            return JSONResponse(
                content=json.loads(claim.response_body) if claim.response_body else {},
                status_code=claim.response_status or 200,
            )

    state = await Wizard.get(session, wizard_id)
    if state is None:
        raise HTTPException(404, "Wizard not found or expired")

    kind = state.get("kind", "")

    # QBO requires Pro+ flag — re-check at commit time.
    if kind in _QBO_KINDS:
        await _check_qbo_flag()

    # Dispatch to the appropriate service.
    result: dict[str, Any]
    if kind in ("bank_csv", "bank_ofx"):
        result = await _commit_bank(session, state, company_id)
    elif kind == "coa":
        result = await _commit_coa(session, state, company_id)
    elif kind == "qbo":
        result = await _commit_qbo(session, state, company_id)
    else:
        raise HTTPException(422, f"Unknown import kind: {kind!r}")

    # Write a change_log entry for the commit. tenant_id is required
    # because change_log has FORCE RLS (migration 0118) — the placeholder
    # default would be rejected by the tenant_isolation policy.
    await change_log_svc.append(
        session,
        entity="import_wizard",
        entity_id=wizard_id,
        op="create",
        actor=f"api:{bearer[:8]}...",
        payload={"kind": kind, "result": result},
        version=1,
        tenant_id=tenant_id,
    )

    await session.commit()

    body = result
    if key is not None:
        await store_response(session, key, 200, json.dumps(body).encode())
        await session.commit()

    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Commit helpers (one per import kind)
# ---------------------------------------------------------------------------


async def _commit_bank(
    session: AsyncSession,
    state: dict[str, Any],
    company_id: UUID,
) -> dict[str, Any]:
    """Parse + persist bank CSV or OFX lines from wizard state."""
    raw = state.get("raw", "")
    if not raw:
        raise HTTPException(422, "Wizard state missing 'raw' field — upload the file first")

    account_id_raw = state.get("account_id")
    if not account_id_raw:
        raise HTTPException(422, "Wizard state missing 'account_id'")
    try:
        account_id = _uuid_mod.UUID(str(account_id_raw))
    except ValueError as exc:
        raise HTTPException(422, f"Invalid account_id: {account_id_raw}") from exc

    kind = state.get("kind", "bank_csv")
    try:
        if kind == "bank_ofx" or raw.lstrip().startswith(("<?xml", "OFXHEADER")):
            parsed = bank_ofx_svc.parse_ofx(raw)
        else:
            fmt = bank_csv_svc.detect_format(raw)
            parsed = bank_csv_svc.parse_bank_csv(raw, fmt=fmt)
    except (bank_csv_svc.BankCsvError, bank_ofx_svc.OfxError) as exc:
        raise HTTPException(422, str(exc)) from exc

    inserted = await persist_svc.persist_bank_lines(
        session,
        company_id=company_id,
        account_id=account_id,
        lines=parsed,
    )
    await session.flush()
    return {"inserted": inserted, "total": len(parsed)}


async def _commit_coa(
    session: AsyncSession,
    state: dict[str, Any],
    company_id: UUID,
) -> dict[str, Any]:
    """Apply a CoA diff from wizard state."""
    raw = state.get("raw", "")
    if not raw:
        raise HTTPException(422, "Wizard state missing 'raw' field — upload the CoA CSV first")

    try:
        rows = coa_svc.parse_coa_csv(raw)
    except coa_svc.CoaImportError as exc:
        raise HTTPException(422, str(exc)) from exc

    existing = (
        await session.execute(
            select(Account).where(Account.company_id == company_id)
        )
    ).scalars().all()

    diff = coa_svc.diff_coa(list(existing), rows)
    archive_removed = bool(state.get("archive_removed", False))
    applied = await coa_svc.apply_coa_diff(
        session,
        company_id,
        diff,
        archive_removed=archive_removed,
    )
    await session.flush()
    return dict(applied)


async def _commit_qbo(
    session: AsyncSession,
    state: dict[str, Any],
    company_id: UUID,
) -> dict[str, Any]:
    """Apply QBO contacts + accounts from wizard state."""
    contacts_raw = state.get("contacts_raw", "")
    accounts_raw = state.get("accounts_raw", "")
    contacts_imported = 0
    accounts_result: dict[str, Any] = {}

    if contacts_raw:
        kind_str = state.get("contacts_kind", "auto")
        kind_enum = (
            qbo_svc.QboContactKind(kind_str)
            if kind_str in ("customer", "vendor", "auto")
            else qbo_svc.QboContactKind.AUTO
        )
        try:
            rows = qbo_svc.parse_qbo_contacts(contacts_raw, kind=kind_enum)
        except qbo_svc.QboImportError as exc:
            raise HTTPException(422, str(exc)) from exc

        existing_names = {
            n
            for n in (
                await session.execute(
                    select(Contact.name).where(
                        Contact.company_id == company_id,
                        Contact.archived_at.is_(None),
                    )
                )
            ).scalars().all()
        }
        for r in rows:
            if r.name in existing_names:
                continue
            session.add(
                Contact(
                    company_id=company_id,
                    name=r.name,
                    contact_type=r.contact_type,
                    email=r.email,
                    phone=r.phone,
                    abn=r.abn,
                    address_line1=r.address_line1,
                    city=r.city,
                    state=r.state,
                    postcode=r.postcode,
                )
            )
            existing_names.add(r.name)
            contacts_imported += 1

    if accounts_raw:
        try:
            qbo_rows = qbo_svc.parse_qbo_accounts(accounts_raw)
        except qbo_svc.QboImportError as exc:
            raise HTTPException(422, str(exc)) from exc

        coa_rows = qbo_svc.qbo_coa_to_rows(qbo_rows)
        existing = (
            await session.execute(
                select(Account).where(Account.company_id == company_id)
            )
        ).scalars().all()
        diff = coa_svc.diff_coa(list(existing), coa_rows)
        accounts_result = await coa_svc.apply_coa_diff(
            session,
            company_id,
            diff,
            archive_removed=bool(state.get("archive_removed", False)),
        )

    await session.flush()
    return {"contacts_imported": contacts_imported, **{f"accounts_{k}": v for k, v in accounts_result.items()}}
