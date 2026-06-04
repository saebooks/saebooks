"""STP Phase 2 payload assembly.

Takes a finalised PayRun and produces the JSON-shaped payload that
will eventually be wrapped in SBR3 XML and submitted to the ATO via
the existing RAM Machine Credential keystore (Phase 3.1).

For now we just BUILD + STORE the payload. The caller (pay-run
finalize) calls ``build_pay_event`` and stores the result in
``stp_submissions`` with status=READY. A future ``submit_event``
function will sign + push to ATO.

Payload shape follows the STP2 spec at:
    https://softwaredevelopers.ato.gov.au/sites/default/files/2024-04/STP%20Phase%202%20Software%20Developer%20Guidance.pdf

Key STP2 fields per payee:
- Payee identifier (TFN, payee_id_bms, previous_payee_id)
- Name (legal), DOB, address (incl. country code)
- Employment basis (F/P/C/L/V/N), income stream type (SAW/CHP/IAA/WHM/...)
- Tax treatment code (6-char string encoding all flags)
- Period totals: gross, tax, allowances[], deductions[], lump_sums{}
- YTD totals: same shape, accumulated since 1 July of current FY
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.employee import Employee
from saebooks.models.pay_run import PayRun, PayRunLine
from saebooks.models.stp_submission import (
    StpEventType,
    StpStatus,
    StpSubmission,
)
from saebooks.models.super_fund import SuperFund
from saebooks.services.lodgement.base import (
    LodgementResult,
    LodgementService,
    LodgementStatus,
)
from saebooks.services.lodgement.exceptions import (
    LodgementError,
    LodgementRejected,
)

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

logger = logging.getLogger(__name__)

# Employer identity fields that MUST be present before a payevent can be
# lodged. The ATO rejects (or silently drops) a payevent whose reporting
# party ABN / legal name / PAYG branch is missing, so we fail fast here
# with a named-field error instead of the old getattr(..., None) fallback.
_REQUIRED_EMPLOYER_FIELDS: tuple[tuple[str, str], ...] = (
    ("abn", "abn"),
    ("legal_name", "legal_name"),
    ("branch_code", "branch"),
)


class StpError(Exception):
    def __init__(self, message: str, *, code: str = "stp_error") -> None:
        super().__init__(message)
        self.code = code


def _dec_str(value: Decimal | None) -> str:
    """STP wire format expects fixed 2dp string for money. None → '0.00'."""
    if value is None:
        return "0.00"
    return f"{value:.2f}"


def _date_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


async def _load_employee(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> Employee | None:
    return await session.get(Employee, employee_id)


async def _load_contact(
    session: AsyncSession, contact_id: uuid.UUID
) -> Contact | None:
    return await session.get(Contact, contact_id)


async def _load_super_fund(
    session: AsyncSession, fund_id: uuid.UUID | None
) -> SuperFund | None:
    if fund_id is None:
        return None
    return await session.get(SuperFund, fund_id)


def _payee_record(
    employee: Employee,
    contact: Contact | None,
    super_fund: SuperFund | None,
    line: PayRunLine,
) -> dict[str, Any]:
    """One payee record in the STP2 payload.

    Sensitive fields (TFN) are NEVER decrypted here — the payload
    carries the ENCRYPTED form; the submission step decrypts at the
    last moment before signing.
    """
    return {
        # Payee identifiers
        "payee_id_bms": str(employee.payee_id_bms),
        "previous_payee_id": employee.previous_payee_id,
        "tfn_encrypted_ref": "see-secure-store",  # signing step replaces with plaintext
        "tfn_status": employee.tfn_status,
        # Identity
        "name": contact.name if contact else "",
        "dob": _date_str(employee.dob),
        # Address (STP2 requires)
        "address": {
            "line1": employee.address_line1,
            "line2": employee.address_line2,
            "suburb": employee.suburb,
            "state": employee.state,
            "postcode": employee.postcode,
            "country_code": employee.country_code,
        },
        # Employment terms
        "employment_basis": employee.employment_basis,
        "start_date": _date_str(employee.start_date),
        "end_date": _date_str(employee.end_date),
        "termination_reason": employee.termination_reason,
        "tax_treatment_code": employee.tax_treatment_code,
        "claims_tax_free_threshold": employee.claims_tax_free_threshold,
        "is_australian_resident": employee.is_australian_resident,
        "study_training_support_loan": employee.study_training_support_loan,
        "working_holiday_maker": employee.working_holiday_maker,
        "whm_country_code": employee.whm_country_code,
        "income_stream_type": employee.income_stream_type,
        "payg_branch_code": employee.payg_branch_code,
        # Super
        "super_fund": (
            {
                "name": super_fund.name,
                "usi": super_fund.usi,
                "is_smsf": super_fund.is_smsf,
                "employer_abn": super_fund.employer_abn,
                "member_number": employee.super_member_number,
            }
            if super_fund
            else None
        ),
        # Period totals (this pay run line)
        "period": {
            "gross": _dec_str(line.gross),
            "tax": _dec_str(line.tax),
            "super": _dec_str(line.super_amount),
            "net": _dec_str(line.net),
            "ordinary_hours": _dec_str(getattr(line, "ordinary_hours", None)),
            "overtime_hours": _dec_str(getattr(line, "overtime_hours", None)),
            "allowances": getattr(line, "allowances", []) or [],
            "deductions": getattr(line, "deductions", []) or [],
            "paid_leave_lines": getattr(line, "paid_leave_lines", []) or [],
            "lump_sums": getattr(line, "lump_sums", {}) or {},
            "extra_pay": _dec_str(getattr(line, "extra_pay", None)),
        },
        # YTD totals (carried forward from prior pay runs in the same FY)
        "ytd": {
            "gross": _dec_str(getattr(line, "ytd_gross", None)),
            "tax": _dec_str(getattr(line, "ytd_tax", None)),
            "super": _dec_str(getattr(line, "ytd_super", None)),
        },
        # PAYG calc audit
        "payg_scale_used": getattr(line, "payg_scale_used", None),
        "payg_breakdown": getattr(line, "payg_breakdown", None),
    }


@dataclass
class StpBuildResult:
    submission: StpSubmission
    payee_count: int
    total_gross: Decimal
    total_tax: Decimal
    total_super: Decimal


async def build_pay_event(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID,
    event_type: StpEventType = StpEventType.PAY,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> StpBuildResult:
    """Assemble + persist the STP payload for a pay run.

    Idempotent: re-running for the same (pay_run_id, event_type)
    creates a NEW submission row but marks the previous one
    SUPERSEDED. Callers should typically build at most one PAY event
    per pay run; corrections go through UPDATE.
    """
    pay_run = await session.get(
        PayRun,
        pay_run_id,
        options=[selectinload(PayRun.lines)],
    )
    if pay_run is None or pay_run.company_id != company_id:
        raise StpError("pay run not found", code="not_found")

    company = await session.get(Company, company_id)
    if company is None:
        raise StpError("company not found", code="company_not_found")

    payees: list[dict[str, Any]] = []
    total_gross = Decimal("0")
    total_tax = Decimal("0")
    total_super = Decimal("0")

    for line in pay_run.lines:
        employee = await _load_employee(session, line.employee_id)
        if employee is None:
            raise StpError(
                f"pay run line {line.id} references missing employee {line.employee_id}",
                code="missing_employee",
            )
        contact = await _load_contact(session, employee.contact_id)
        super_fund = await _load_super_fund(session, employee.super_fund_id)
        payees.append(_payee_record(employee, contact, super_fund, line))
        total_gross += line.gross or Decimal("0")
        total_tax += line.tax or Decimal("0")
        total_super += line.super_amount or Decimal("0")

    payload: dict[str, Any] = {
        "schema_version": "STP2-1.0",
        "submission_software": {
            "name": "SAE Books",
            "version": "v2026.05",
        },
        "employer": {
            "abn": getattr(company, "abn", None),
            "legal_name": getattr(company, "legal_name", None)
            or getattr(company, "name", None),
            "branch_code": "001",
        },
        "report_period": {
            "start": _date_str(pay_run.period_start),
            "end": _date_str(pay_run.period_end),
            "payment_date": _date_str(pay_run.payment_date),
        },
        "event_type": event_type.value if hasattr(event_type, "value") else str(event_type),
        "payees": payees,
        "totals": {
            "gross": _dec_str(total_gross),
            "tax": _dec_str(total_tax),
            "super": _dec_str(total_super),
            "payee_count": len(payees),
        },
        "assembled_at": datetime.now(UTC).isoformat(),
    }

    # Mark any prior PAY submission for this pay_run as SUPERSEDED.
    if event_type == StpEventType.PAY:
        prior = await session.execute(
            sa.select(StpSubmission).where(
                StpSubmission.company_id == company_id,
                StpSubmission.pay_run_id == pay_run_id,
                StpSubmission.event_type == StpEventType.PAY.value,
                StpSubmission.status.in_(
                    [StpStatus.READY.value, StpStatus.SUBMITTED.value]
                ),
            )
        )
        for old in prior.scalars().all():
            old.status = StpStatus.SUPERSEDED.value
            old.version += 1

    submission = StpSubmission(
        company_id=company_id,
        tenant_id=tenant_id,
        pay_run_id=pay_run_id,
        event_type=(
            event_type.value if hasattr(event_type, "value") else str(event_type)
        ),
        status=StpStatus.READY.value,
        payload=payload,
        errors=[],
    )
    session.add(submission)
    await session.flush()
    await session.refresh(submission)

    return StpBuildResult(
        submission=submission,
        payee_count=len(payees),
        total_gross=total_gross,
        total_tax=total_tax,
        total_super=total_super,
    )


async def list_for_pay_run(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID,
) -> list[StpSubmission]:
    stmt = (
        sa.select(StpSubmission)
        .where(
            StpSubmission.company_id == company_id,
            StpSubmission.pay_run_id == pay_run_id,
        )
        .order_by(StpSubmission.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_company(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StpSubmission], int]:
    count = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(StpSubmission)
            .where(StpSubmission.company_id == company_id)
        )
    ).scalar_one()
    items = list(
        (
            await session.execute(
                sa.select(StpSubmission)
                .where(StpSubmission.company_id == company_id)
                .order_by(StpSubmission.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return items, int(count)


# --------------------------------------------------------------------------- #
# STP2 submit orchestration (gate-independent half)
# --------------------------------------------------------------------------- #


def _validate_employer(employer: dict[str, Any] | None) -> None:
    """Raise ``StpError`` if the employer block is missing a mandatory field.

    Called from the submit path BEFORE any envelope is built or lodged, so
    an incompletely-configured company is rejected with a clear, named-field
    error rather than the old ``getattr(company, "abn", None)`` fallback that
    would have shipped ``None`` to the ATO.
    """
    employer = employer or {}
    missing: list[str] = []
    for key, label in _REQUIRED_EMPLOYER_FIELDS:
        value = employer.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(label)
    if missing:
        raise StpError(
            "employer details incomplete — missing: " + ", ".join(missing),
            code="employer_invalid",
        )


def _default_document_builder(payload: dict[str, Any]) -> bytes:
    """Resolve the gated PAYEVNT XBRL generator lazily.

    The taxonomy generator (``build_stp_pay_event_document``) is gated on
    the ATO PVT reference pack and lives on a separate branch. We import it
    lazily so this module loads on branches where it is absent; tests inject
    a stub ``document_builder`` and never hit this path. If a real submit is
    attempted on a branch without the generator, fail loudly rather than
    sending a half-formed envelope.
    """
    try:  # pragma: no cover - exercised only with the gated generator present
        from saebooks.services.lodgement.sbr.stp import (
            build_stp_pay_event_document,
        )
    except ImportError as exc:  # pragma: no cover
        raise StpError(
            "PAYEVNT XBRL generator unavailable on this build — the SBR "
            "taxonomy half is gated on the ATO PVT reference pack.",
            code="generator_unavailable",
        ) from exc
    return build_stp_pay_event_document(payload)


def _resolve_tfns_for_envelope(
    payload: dict[str, Any],
    tfn_resolver: Callable[[str], str],
) -> dict[str, Any]:
    """Build a SUBMIT-ONLY copy of the payload with TFNs decrypted.

    The stored ``StpSubmission.payload`` carries only ``tfn_encrypted_ref``
    placeholders — plaintext TFNs are NEVER persisted. At the last moment
    before signing we decrypt them into a throwaway dict that is handed to
    the document builder and then discarded; it is never written back to the
    row and never logged. The decrypt itself goes through the injected seam
    (``tfn_resolver``, default ``crypto.decrypt_field``) so the boundary is
    explicit and stubbable in tests.
    """
    import copy

    submit_payload = copy.deepcopy(payload)
    for payee in submit_payload.get("payees", []) or []:
        ref = payee.get("tfn_encrypted_ref")
        if ref:
            # Decrypted plaintext lives ONLY in this throwaway dict.
            payee["tfn"] = tfn_resolver(ref)
            payee.pop("tfn_encrypted_ref", None)
    return submit_payload


@dataclass
class StpSubmitResult:
    status: str
    ato_receipt_number: str | None
    submission_id: uuid.UUID
    errors: list[dict[str, Any]]


async def submit_event(
    session: AsyncSession,
    submission_id: uuid.UUID,
    *,
    lodgement_service: LodgementService,
    document_builder: Callable[[dict[str, Any]], bytes] | None = None,
    tfn_resolver: Callable[[str], str] | None = None,
    submitted_by: uuid.UUID | None = None,
) -> StpSubmitResult:
    """Drive a READY ``StpSubmission`` through the STP2 lodgement state machine.

    READY -> SUBMITTED -> (ACCEPTED | REJECTED).

    Steps:
      1. Load the submission; validate it is in a submittable state.
      2. Idempotency: an ACCEPTED submission returns its cached receipt
         WITHOUT re-lodging.
      3. Validate the employer block (named-field error if incomplete).
      4. Decrypt TFNs into a throwaway submit-only payload (never stored).
      5. Render the SBR3 envelope via ``document_builder`` (gated generator).
      6. ``lodge_stp`` the envelope (idempotency key = submission id).
      7. On success store ``ato_receipt_number`` + ``submitted_at`` and go
         ACCEPTED; on ``LodgementRejected`` record errors and go REJECTED.

    The lodgement call NEVER touches a real ATO in tests — callers inject a
    ``NullLodgementService`` or a deterministic test double.
    """
    submission = await session.get(StpSubmission, submission_id)
    if submission is None:
        raise StpError("submission not found", code="not_found")

    # (c) Idempotency — already accepted: return the cached receipt, no re-lodge.
    if submission.status == StpStatus.ACCEPTED.value:
        return StpSubmitResult(
            status=submission.status,
            ato_receipt_number=submission.ato_receipt_number,
            submission_id=submission.id,
            errors=list(submission.errors or []),
        )

    # (d) Only a READY submission may be lodged. SUBMITTED (in-flight),
    # REJECTED, and SUPERSEDED are all non-submittable here.
    if submission.status != StpStatus.READY.value:
        raise StpError(
            f"submission in state {submission.status} is not submittable "
            f"(expected READY)",
            code="invalid_state",
        )

    payload = submission.payload or {}

    # (1) Employer pre-submit validation — fail fast, before any lodge.
    _validate_employer(payload.get("employer"))

    # (4) Decrypt TFNs into a throwaway copy (never persisted, never logged).
    builder = document_builder or _default_document_builder
    resolver = tfn_resolver
    if resolver is None:
        from saebooks.services.crypto import decrypt_field as resolver  # type: ignore

    submit_payload = _resolve_tfns_for_envelope(payload, resolver)

    # (5) Render the SBR3 envelope. The builder is the gated taxonomy seam.
    envelope = builder(submit_payload)

    # Mark in-flight before the relay call so a crash mid-lodge leaves a
    # SUBMITTED row (operator can reconcile) rather than a stuck READY.
    submission.status = StpStatus.SUBMITTED.value
    submission.submitted_at = datetime.now(UTC)
    if submitted_by is not None:
        submission.submitted_by = submitted_by
    await session.flush()

    metadata = {
        "submission_id": str(submission.id),
        "company_id": str(submission.company_id),
        "pay_run_id": str(submission.pay_run_id),
        "event_type": submission.event_type,
    }

    # (6) Relay call. payevent_id == submission id is the server-side 24h
    # dedup key, so a network retry never double-lodges.
    try:
        result: LodgementResult = await lodgement_service.lodge_stp(
            envelope, str(submission.id), metadata
        )
    except LodgementRejected as exc:
        # (b) ATO 422 — record errors, transition REJECTED, do NOT accept.
        submission.status = StpStatus.REJECTED.value
        submission.errors = exc.ato_errors or [{"message": exc.detail}]
        submission.ato_response_payload = exc.raw_response or None
        submission.ato_receipt_number = None
        await session.flush()
        logger.info(
            "STP submission %s rejected by ATO (%d errors)",
            submission.id,
            len(submission.errors),
        )
        return StpSubmitResult(
            status=submission.status,
            ato_receipt_number=None,
            submission_id=submission.id,
            errors=list(submission.errors),
        )
    except LodgementError:
        # Auth / edition / upstream-5xx: leave the row SUBMITTED (in-flight)
        # so the operator can retry; re-raise for the caller to surface.
        await session.flush()
        raise

    # (a) Success — store receipt + accept. QUEUED keeps it SUBMITTED until a
    # later poll resolves it; only an ACCEPTED relay outcome flips ACCEPTED.
    submission.ato_receipt_number = result.ato_receipt_id
    submission.ato_response_payload = result.raw_response or None
    submission.errors = []
    if result.status == LodgementStatus.ACCEPTED:
        submission.status = StpStatus.ACCEPTED.value
    else:
        submission.status = StpStatus.SUBMITTED.value
    await session.flush()

    logger.info(
        "STP submission %s lodged: status=%s receipt=%s",
        submission.id,
        submission.status,
        submission.ato_receipt_number,
    )
    return StpSubmitResult(
        status=submission.status,
        ato_receipt_number=submission.ato_receipt_number,
        submission_id=submission.id,
        errors=list(submission.errors or []),
    )
