"""ATO SBR v1 API router — Cat-C greenfield mirror (W3).

Mounted at ``/api/v1/ato_sbr``.  All routes require:

* Bearer auth (JWT app session or static dev token).
* ``FLAG_ATO_SBR`` feature flag (Pro+ edition) — returns 404 to
  lower tiers so the endpoints aren't even advertised.

Endpoints
---------
POST   /ato_sbr/keystore                    — upload Machine Credential PFX
GET    /ato_sbr/keystore                    — list keystore entries
DELETE /ato_sbr/keystore/{id}              — soft-delete (archived_at)
POST   /ato_sbr/onboarding/wizards         — start an onboarding wizard
POST   /ato_sbr/onboarding/wizards/{id}/step — advance wizard one step
POST   /ato_sbr/ping                        — smoke-test the lodge-server

Storage note
------------
The brief refers to a separate ``ato_sbr_keystore`` table.  That table
does not exist in this codebase — keystore data lives in
``ato_sbr_configs`` (one row per company, columns
``keystore_encrypted``, ``keystore_password_encrypted``, etc.).

We model the "keystore entry" concept over the existing
``ato_sbr_configs`` table: the config row's ``id`` acts as the keystore
entry id.  Soft-delete clears keystore columns (sets them to NULL) but
keeps the config row (which holds other onboarding state).  This is
consistent with the existing ``clear_config`` service helper.

Ping
----
Uses ``saebooks.services.lodgement.remote.RemoteLodgementService``
indirectly via the existing ``get_lodgement_service`` factory from
``saebooks.api.v1.deps``.  When lodge-server is in stub mode (returns
501) we surface ``{ok: false, reason: "lodge_server_stub_mode"}``.
The dedicated ``/api/v1/ato_sbr/ping`` endpoint accepts a
``keystore_id`` (the config row UUID) for forward-compatibility — we
validate the id belongs to the tenant's config, but do not yet pass
the credential through to the lodge-server (the lodge-server auth is
by licence JWT, not by PFX, in the current architecture).
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1._wizard import Wizard, WizardExpiredError, WizardNotFoundError
from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.config import settings
from saebooks.models.ato_sbr import AtoSbrConfig
from saebooks.models.company import Company
from saebooks.services import crypto as crypto_svc
from saebooks.services.ato_sbr import onboarding as sbr
from saebooks.services.ato_sbr.keystore import KeystoreError, load_keystore
from saebooks.services.features import FLAG_ATO_SBR, require_feature
from saebooks.services.lodgement.exceptions import (
    LodgementAuthError,
    LodgementError,
    LodgementUpstreamUnavailable,
)
from saebooks.services.lodgement.remote import RemoteLodgementService

router = APIRouter(
    prefix="/ato_sbr",
    tags=["ato_sbr"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_ATO_SBR)),
    ],
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _active_company_id(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Return the first active company for the tenant."""
    result = await session.execute(
        select(Company)
        .where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
        .order_by(Company.created_at)
    )
    company = result.scalars().first()
    if company is None:
        raise HTTPException(404, "No active company for tenant")
    return company.id


async def _get_or_create_config(
    session: AsyncSession, company_id: uuid.UUID
) -> AtoSbrConfig:
    """Fetch or lazily create the ato_sbr_configs row for this company."""
    return await sbr.get_or_create_config(session, company_id)


def _keystore_entry(config: AtoSbrConfig) -> dict[str, Any]:
    """Serialise config fields into the keystore-entry shape."""
    return {
        "id": str(config.id),
        "label": config.keystore_subject_cn or config.keystore_filename or "unnamed",
        "abn_or_name": config.keystore_subject_cn,
        "expires_at": (
            config.keystore_not_after.isoformat()
            if config.keystore_not_after
            else None
        ),
        "archived_at": None,  # no soft-delete column; None means active
        "filename": config.keystore_filename,
        "issuer_cn": config.keystore_issuer_cn,
        "serial": config.keystore_serial,
        "not_before": (
            config.keystore_not_before.isoformat()
            if config.keystore_not_before
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Wizard flow definitions
# ---------------------------------------------------------------------------

_MACHINE_CREDENTIAL_STEPS: list[dict[str, Any]] = [
    {
        "step": 0,
        "name": "mygovid",
        "label": "Set up myGovID",
        "description": (
            "Ensure you have myGovID installed with Strong identity strength."
        ),
        "questions": [
            {
                "field": "mygovid_confirmed",
                "type": "checkbox",
                "label": "I have set up myGovID with Strong identity strength",
            }
        ],
    },
    {
        "step": 1,
        "name": "ram_authority",
        "label": "Link Principal Authority in RAM",
        "description": "Link Principal Authority for the active company's ABN in RAM.",
        "questions": [
            {
                "field": "ram_confirmed",
                "type": "checkbox",
                "label": "I have linked Principal Authority in RAM",
            }
        ],
    },
    {
        "step": 2,
        "name": "downloader",
        "label": "Install Machine Credential Downloader",
        "description": "Install the Machine Credential Downloader Chrome extension.",
        "questions": [
            {
                "field": "downloader_confirmed",
                "type": "checkbox",
                "label": "I have installed the Machine Credential Downloader extension",
            }
        ],
    },
    {
        "step": 3,
        "name": "keystore",
        "label": "Upload Machine Credential",
        "description": "Upload the keystore.xml file exported by the Downloader extension.",
        "questions": [
            {
                "field": "keystore_done",
                "type": "info",
                "label": "Use POST /api/v1/ato_sbr/keystore to upload your keystore file",
            }
        ],
    },
    {
        "step": 4,
        "name": "ssid",
        "label": "Record Software Service ID",
        "description": "Enter the SSID provided by the ATO Software Developer program.",
        "questions": [
            {
                "field": "ssid",
                "type": "text",
                "label": "Software Service ID (SSID)",
            }
        ],
    },
]

_SSID_LINK_STEPS: list[dict[str, Any]] = [
    {
        "step": 0,
        "name": "ssid_entry",
        "label": "Enter SSID",
        "description": "Enter the Software Service ID provided by the ATO.",
        "questions": [
            {
                "field": "ssid",
                "type": "text",
                "label": "Software Service ID (SSID)",
            }
        ],
    },
    {
        "step": 1,
        "name": "ssid_confirm",
        "label": "Confirm SSID",
        "description": "Confirm the SSID is correct before saving.",
        "questions": [
            {
                "field": "confirmed",
                "type": "checkbox",
                "label": "I confirm the SSID is correct",
            }
        ],
    },
]

_FLOW_STEPS: dict[str, list[dict[str, Any]]] = {
    "machine_credential": _MACHINE_CREDENTIAL_STEPS,
    "ssid_link": _SSID_LINK_STEPS,
}


def _current_step_response(
    wizard_id: uuid.UUID,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Build the response for the current wizard step."""
    flow = state.get("flow", "machine_credential")
    steps = _FLOW_STEPS.get(flow, _MACHINE_CREDENTIAL_STEPS)
    step_index = int(state.get("step", 0))

    if step_index >= len(steps):
        return {
            "wizard_id": str(wizard_id),
            "status": "complete",
            "flow": flow,
        }

    step_def = steps[step_index]
    return {
        "wizard_id": str(wizard_id),
        "status": "in_progress",
        "flow": flow,
        "step_index": step_index,
        "step_count": len(steps),
        "current_step": step_def,
    }


def _validate_step_answers(
    flow: str,
    step_index: int,
    answers: dict[str, Any],
) -> str | None:
    """Return an error string if answers are invalid, else None."""
    steps = _FLOW_STEPS.get(flow, _MACHINE_CREDENTIAL_STEPS)
    if step_index >= len(steps):
        return None  # already complete
    step_def = steps[step_index]
    for q in step_def.get("questions", []):
        field = q["field"]
        qtype = q.get("type", "text")
        if qtype == "info":
            continue  # informational only, no answer required
        if qtype == "checkbox":
            if not answers.get(field):
                return f"Field '{field}' must be confirmed"
        elif qtype == "text":
            val = answers.get(field, "")
            if not val or not str(val).strip():
                return f"Field '{field}' is required"
    return None


# ---------------------------------------------------------------------------
# POST /ato_sbr/keystore — upload Machine Credential
# ---------------------------------------------------------------------------


@router.post("/keystore", status_code=201)
async def upload_keystore(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(...),
    label: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Upload a RAM Machine Credential PFX/keystore file.

    Multipart form fields:
    - ``file``:     The keystore file (PFX or ATO XML format).
    - ``password``: Decryption password.
    - ``label``:    Optional human-readable label (defaults to subject CN).

    The file is parsed, cert metadata extracted, then the raw bytes and
    password are encrypted at rest via ``saebooks.services.crypto``.
    Returns 201 with the keystore entry on success.
    """
    # Validate inputs before checking server-side encryption configuration —
    # an empty file is a 422 client error regardless of whether the server
    # is configured, so the test expects 422 to take precedence over 503.
    data = await file.read()
    if not data:
        raise HTTPException(422, "No file content uploaded")

    if not crypto_svc.is_configured(settings):
        raise HTTPException(
            503,
            "SAEBOOKS_FIELD_ENCRYPTION_KEY is not configured — "
            "cannot store keystore without at-rest encryption",
        )

    try:
        loaded = load_keystore(data, password)
    except KeystoreError as exc:
        raise HTTPException(422, f"Keystore parse failed: {exc}") from exc

    tenant_id = resolve_tenant_id(request)
    company_id = await _active_company_id(session, tenant_id)
    config = await _get_or_create_config(session, company_id)

    # Encrypt and store.
    config.keystore_encrypted = crypto_svc.encrypt_field(
        data.decode("latin-1"), settings=settings
    )
    config.keystore_password_encrypted = crypto_svc.encrypt_field(
        password, settings=settings
    )
    config.keystore_filename = file.filename or "keystore.xml"
    config.keystore_subject_cn = loaded.subject_cn
    config.keystore_issuer_cn = loaded.issuer_cn
    config.keystore_serial = loaded.serial
    config.keystore_not_before = loaded.not_before
    config.keystore_not_after = loaded.not_after

    await session.flush()
    await session.commit()
    await session.refresh(config)

    return JSONResponse(_keystore_entry(config), status_code=201)


# ---------------------------------------------------------------------------
# GET /ato_sbr/keystore — list keystore entries
# ---------------------------------------------------------------------------


@router.get("/keystore")
async def list_keystore(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """List keystore entries for the tenant.

    Returns a list of keystore entry objects.  Entries without an
    uploaded keystore (``keystore_encrypted IS NULL``) are excluded.
    """
    tenant_id = resolve_tenant_id(request)
    # Fetch all companies for this tenant, then their configs.
    companies_result = await session.execute(
        select(Company).where(
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
    )
    company_ids = [c.id for c in companies_result.scalars().all()]

    if not company_ids:
        return JSONResponse({"items": []})

    configs_result = await session.execute(
        select(AtoSbrConfig).where(
            AtoSbrConfig.company_id.in_(company_ids),
            AtoSbrConfig.keystore_encrypted.is_not(None),
        )
    )
    configs = configs_result.scalars().all()

    return JSONResponse(
        {"items": [_keystore_entry(c) for c in configs]}
    )


# ---------------------------------------------------------------------------
# DELETE /ato_sbr/keystore/{id} — soft-delete (clear keystore fields)
# ---------------------------------------------------------------------------


@router.delete("/keystore/{entry_id}", status_code=204)
async def delete_keystore(
    entry_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Soft-delete a keystore entry (clears keystore fields, keeps config row).

    Returns 404 if the entry doesn't exist for this tenant, or 409 if
    it has already been cleared (no active keystore to delete).
    """
    tenant_id = resolve_tenant_id(request)

    # Find config by id + tenant scope (via company join).
    result = await session.execute(
        select(AtoSbrConfig)
        .join(Company, AtoSbrConfig.company_id == Company.id)
        .where(
            AtoSbrConfig.id == entry_id,
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
    )
    config = result.scalars().first()
    if config is None:
        raise HTTPException(404, "Keystore entry not found")

    if config.keystore_encrypted is None:
        raise HTTPException(409, "Keystore entry is already archived (no active keystore)")

    # Clear keystore fields — the config row stays for other onboarding state.
    config.keystore_encrypted = None
    config.keystore_password_encrypted = None
    config.keystore_filename = None
    config.keystore_subject_cn = None
    config.keystore_issuer_cn = None
    config.keystore_serial = None
    config.keystore_not_before = None
    config.keystore_not_after = None

    await session.flush()
    await session.commit()
    return JSONResponse(None, status_code=204)


# ---------------------------------------------------------------------------
# POST /ato_sbr/onboarding/wizards — start a new onboarding wizard
# ---------------------------------------------------------------------------


@router.post("/onboarding/wizards", status_code=201)
async def start_wizard(
    request: Request,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Start a new ATO SBR onboarding wizard session.

    Request body::

        {"flow": "machine_credential" | "ssid_link"}

    Returns ``{wizard_id, status, flow, step_index, current_step}``
    describing the first step.
    """
    flow = body.get("flow", "machine_credential")
    if flow not in _FLOW_STEPS:
        raise HTTPException(
            422,
            f"Unknown flow {flow!r}. Must be one of: {list(_FLOW_STEPS)}",
        )

    initial_state: dict[str, Any] = {"flow": flow, "step": 0, "answers": {}}
    wizard_id = await Wizard.start(
        session,
        kind="ato_sbr",
        initial_state=initial_state,
        ttl_seconds=7200,  # 2-hour window for the onboarding wizard
    )
    await session.commit()

    return JSONResponse(
        _current_step_response(wizard_id, initial_state),
        status_code=201,
    )


# ---------------------------------------------------------------------------
# POST /ato_sbr/onboarding/wizards/{id}/step — advance wizard
# ---------------------------------------------------------------------------


@router.post("/onboarding/wizards/{wizard_id}/step")
async def advance_wizard(
    wizard_id: uuid.UUID,
    request: Request,
    body: dict[str, Any],
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Submit answers for the current step and advance the wizard.

    Request body::

        {"answers": {"<field>": <value>, ...}}

    Validates the current step's required fields server-side.

    Optimistic locking via ``If-Match: <step_index>``.  If the client's
    step index doesn't match the stored step the server returns 409.

    Returns the next step's questions, or ``{status: "complete"}`` when
    all steps are done.
    """
    current_state = await Wizard.get(session, wizard_id)
    if current_state is None:
        raise HTTPException(404, "Wizard not found or expired")

    current_step = int(current_state.get("step", 0))
    flow = current_state.get("flow", "machine_credential")

    # Optimistic locking check.
    if if_match is not None:
        try:
            expected = int(if_match.strip().strip('"'))
        except ValueError as exc:
            raise HTTPException(400, "If-Match must be a step index integer") from exc
        if expected != current_step:
            return JSONResponse(
                {
                    "detail": "stale_step",
                    "current_step": current_step,
                    "submitted_step": expected,
                },
                status_code=409,
            )

    answers = body.get("answers", {})

    # Server-side validation.
    error = _validate_step_answers(flow, current_step, answers)
    if error:
        raise HTTPException(422, error)

    _FLOW_STEPS.get(flow, _MACHINE_CREDENTIAL_STEPS)
    next_step = current_step + 1

    # Merge answers and advance.
    all_answers = {**current_state.get("answers", {}), **answers}
    patch: dict[str, Any] = {
        "step": next_step,
        "answers": all_answers,
    }

    try:
        new_state = await Wizard.step(session, wizard_id, patch_state=patch)
    except WizardNotFoundError as exc:
        raise HTTPException(404, "Wizard not found") from exc
    except WizardExpiredError as exc:
        raise HTTPException(410, "Wizard session has expired") from exc

    await session.commit()

    return JSONResponse(_current_step_response(wizard_id, new_state))


# ---------------------------------------------------------------------------
# POST /ato_sbr/ping — test lodge-server connection
# ---------------------------------------------------------------------------


@router.post("/ping")
async def ping_lodge_server(
    request: Request,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Test the lodge-server connection using a chosen keystore entry.

    Request body::

        {"keystore_id": "<uuid>"}

    The ``keystore_id`` is the ``ato_sbr_configs.id`` UUID.  We verify
    it belongs to the requesting tenant before attempting the ping.

    Returns::

        {
          "ok": true | false,
          "lodge_server_version": "...",
          "latency_ms": 123,
          "server_time": "2026-05-04T12:00:00+00:00"
        }

    When the lodge-server is in stub mode (returns 501)::

        {"ok": false, "reason": "lodge_server_stub_mode"}
    """
    keystore_id_raw = body.get("keystore_id")
    if not keystore_id_raw:
        raise HTTPException(422, "keystore_id is required")
    try:
        keystore_id = uuid.UUID(str(keystore_id_raw))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "keystore_id must be a valid UUID") from exc

    tenant_id = resolve_tenant_id(request)

    # Validate keystore entry belongs to this tenant.
    result = await session.execute(
        select(AtoSbrConfig)
        .join(Company, AtoSbrConfig.company_id == Company.id)
        .where(
            AtoSbrConfig.id == keystore_id,
            Company.tenant_id == tenant_id,
            Company.archived_at.is_(None),
        )
    )
    config = result.scalars().first()
    if config is None:
        raise HTTPException(404, "Keystore entry not found")
    if config.keystore_encrypted is None:
        raise HTTPException(409, "Keystore entry has been archived — no active credential to test")

    # Attempt a health check against lodge-server.
    # The lodge-server auth is by licence JWT; we do a lightweight
    # GET /api/v1/health (or audit log) as the connectivity check.
    svc = RemoteLodgementService()
    start = time.monotonic()
    try:
        # Use my_audit_log as a lightweight authenticated probe —
        # it's the cheapest authenticated endpoint on lodge-server.
        await svc.my_audit_log(limit=1)
        latency_ms = int((time.monotonic() - start) * 1000)
        return JSONResponse({
            "ok": True,
            "lodge_server_version": None,
            "latency_ms": latency_ms,
            "server_time": datetime.now(UTC).isoformat(),
        })
    except LodgementAuthError as exc:
        # Lodge-server rejected our licence token — surface that clearly.
        latency_ms = int((time.monotonic() - start) * 1000)
        return JSONResponse({
            "ok": False,
            "reason": "lodge_server_auth_error",
            "detail": exc.detail,
            "latency_ms": latency_ms,
        })
    except LodgementUpstreamUnavailable as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        # Detect stub mode: 501 maps to STUB status in the main lodge methods,
        # but my_audit_log raises UpstreamUnavailable on non-200. We detect the
        # stub case by checking if status==501 is surfaced.
        if exc.status == 501:
            return JSONResponse({
                "ok": False,
                "reason": "lodge_server_stub_mode",
                "latency_ms": latency_ms,
            })
        return JSONResponse({
            "ok": False,
            "reason": "lodge_server_unavailable",
            "detail": exc.detail,
            "latency_ms": latency_ms,
        })
    except LodgementError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return JSONResponse({
            "ok": False,
            "reason": "lodge_server_error",
            "detail": str(exc),
            "latency_ms": latency_ms,
        })
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        return JSONResponse({
            "ok": False,
            "reason": "unexpected_error",
            "detail": str(exc),
            "latency_ms": latency_ms,
        })
