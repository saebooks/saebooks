"""TDD coverage for STP2 ``submit_event`` orchestration + employer validation.

This is the GATE-INDEPENDENT half of STP lodgement: the state machine
(READY -> SUBMITTED -> ACCEPTED|REJECTED), employer pre-submit validation,
idempotency, and the relay-call orchestration. The XBRL PAYEVNT payload
TAXONOMY generator (``build_stp_pay_event_document``) is gated on the ATO
PVT pack and lives on a separate branch; here we inject it as a seam
(``document_builder``) with a deterministic stub, and inject a test-double
``LodgementService`` so NO real ATO transmit ever happens.

Style mirrors tests/api/v1/test_stp_submissions.py: seed StpSubmission rows
directly via ORM (faking the upstream pay-run finalize) under a freshly
created, isolated company so we can mutate employer fields without touching
the shared seed company.
"""
from __future__ import annotations

import uuid
from datetime import date as _date
from datetime import datetime

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.pay_run import PayRun
from saebooks.models.stp_submission import StpStatus, StpSubmission
from saebooks.services import stp as stp_svc
from saebooks.services.lodgement.base import LodgementResult, LodgementStatus
from saebooks.services.lodgement.exceptions import LodgementRejected

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _RecordingLodgement:
    """Deterministic LodgementService double. Records every lodge_stp call."""

    def __init__(self, result: LodgementResult | None = None) -> None:
        self.calls: list[tuple[bytes, str, dict]] = []
        self._result = result or LodgementResult(
            status=LodgementStatus.ACCEPTED,
            ato_receipt_id="ATO-RECEIPT-123",
            ato_timestamp=datetime(2026, 4, 10, 1, 2, 3),
            warnings=[],
            raw_response={"status": "accepted", "receipt": "ATO-RECEIPT-123"},
        )

    async def lodge_stp(self, envelope, payevent_id, metadata):
        self.calls.append((envelope, payevent_id, metadata))
        return self._result


class _RejectingLodgement:
    """LodgementService double whose lodge_stp signals an ATO 422 rejection."""

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, dict]] = []

    async def lodge_stp(self, envelope, payevent_id, metadata):
        self.calls.append((envelope, payevent_id, metadata))
        raise LodgementRejected(
            "ATO rejected payevent",
            ato_errors=[{"code": "CMN.ATO.GEN.XML03", "message": "bad TFN"}],
            raw_response={"status": "rejected"},
        )


def _stub_builder(payload: dict) -> bytes:
    """Stand-in for the gated build_stp_pay_event_document generator."""
    return b"<payevnt>stub-envelope</payevnt>"


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


async def _seed_company(*, abn: str | None = "12345678901",
                        legal_name: str | None = "Acme Pty Ltd",
                        branch: str | None = "001") -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        c = Company(
            tenant_id=_DEFAULT_TENANT_ID,
            name="Acme",
            legal_name=legal_name,
            abn=abn,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        cid = c.id
    return cid, branch


async def _seed_pay_run(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        run = PayRun(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            period_start=_date(2026, 4, 1),
            period_end=_date(2026, 4, 7),
            payment_date=_date(2026, 4, 10),
            description="submit_event pytest run",
            status="draft",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def _seed_submission(
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID,
    *,
    employer: dict | None = None,
    status: str = "READY",
) -> uuid.UUID:
    if employer is None:
        employer = {"abn": "12345678901", "legal_name": "Acme Pty Ltd",
                    "branch_code": "001"}
    async with AsyncSessionLocal() as session:
        sub = StpSubmission(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            pay_run_id=pay_run_id,
            event_type="PAY",
            status=status,
            payload={
                "employer": employer,
                "payees": [{"payee_id_bms": "EE1",
                            "tfn_encrypted_ref": "see-secure-store"}],
                "totals": {"gross": "1000.00", "tax": "200.00",
                           "payee_count": 1},
            },
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return sub.id


async def _get_sub(sub_id: uuid.UUID) -> StpSubmission:
    async with AsyncSessionLocal() as session:
        return await session.get(StpSubmission, sub_id)


# --------------------------------------------------------------------------- #
# Task 1 — employer pre-submit validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_submit_rejects_missing_abn() -> None:
    cid, _ = await _seed_company(abn=None)
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(
        cid, run_id, employer={"abn": None, "legal_name": "Acme Pty Ltd",
                               "branch_code": "001"})
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        with pytest.raises(stp_svc.StpError) as ei:
            await stp_svc.submit_event(
                session, sub_id,
                lodgement_service=lodge,
                document_builder=_stub_builder,
                tfn_resolver=lambda ref: "111111111",
            )
    assert "abn" in str(ei.value).lower()
    # Must NOT have lodged a malformed employer.
    assert lodge.calls == []


@pytest.mark.asyncio
async def test_submit_rejects_missing_legal_name() -> None:
    cid, _ = await _seed_company(legal_name=None)
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(
        cid, run_id, employer={"abn": "12345678901", "legal_name": None,
                               "branch_code": "001"})
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        with pytest.raises(stp_svc.StpError) as ei:
            await stp_svc.submit_event(
                session, sub_id,
                lodgement_service=lodge,
                document_builder=_stub_builder,
                tfn_resolver=lambda ref: "111111111",
            )
    assert "legal_name" in str(ei.value).lower() or "legal name" in str(ei.value).lower()
    assert lodge.calls == []


@pytest.mark.asyncio
async def test_submit_rejects_missing_branch() -> None:
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(
        cid, run_id, employer={"abn": "12345678901",
                               "legal_name": "Acme Pty Ltd",
                               "branch_code": None})
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        with pytest.raises(stp_svc.StpError) as ei:
            await stp_svc.submit_event(
                session, sub_id,
                lodgement_service=lodge,
                document_builder=_stub_builder,
                tfn_resolver=lambda ref: "111111111",
            )
    assert "branch" in str(ei.value).lower()
    assert lodge.calls == []


# --------------------------------------------------------------------------- #
# Task 2 — submit_event state machine
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_submit_ready_to_accepted() -> None:
    """(a) READY -> SUBMITTED -> ACCEPTED, receipt + timestamp stored."""
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        result = await stp_svc.submit_event(
            session, sub_id,
            lodgement_service=lodge,
            document_builder=_stub_builder,
            tfn_resolver=lambda ref: "111111111",
        )
        await session.commit()
    assert len(lodge.calls) == 1
    envelope, payevent_id, _meta = lodge.calls[0]
    assert envelope == b"<payevnt>stub-envelope</payevnt>"
    # idempotency key is the submission id
    assert payevent_id == str(sub_id)
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.ACCEPTED.value
    assert sub.ato_receipt_number == "ATO-RECEIPT-123"
    assert sub.submitted_at is not None
    assert result.status == StpStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_submit_rejection_records_errors() -> None:
    """(b) ATO 422 -> REJECTED with errors recorded, not ACCEPTED."""
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    lodge = _RejectingLodgement()
    async with AsyncSessionLocal() as session:
        result = await stp_svc.submit_event(
            session, sub_id,
            lodgement_service=lodge,
            document_builder=_stub_builder,
            tfn_resolver=lambda ref: "111111111",
        )
        await session.commit()
    assert len(lodge.calls) == 1
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.REJECTED.value
    assert sub.ato_receipt_number is None
    assert sub.errors, "expected ATO errors recorded"
    assert any(e.get("code") == "CMN.ATO.GEN.XML03" for e in sub.errors)
    assert result.status == StpStatus.REJECTED.value


@pytest.mark.asyncio
async def test_submit_idempotent_on_accepted() -> None:
    """(c) Re-submitting an ACCEPTED submission returns cached receipt,
    does NOT re-lodge."""
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        await stp_svc.submit_event(
            session, sub_id, lodgement_service=lodge,
            document_builder=_stub_builder, tfn_resolver=lambda ref: "111111111")
        await session.commit()
    # Second call — must be a no-op lodge.
    async with AsyncSessionLocal() as session:
        result2 = await stp_svc.submit_event(
            session, sub_id, lodgement_service=lodge,
            document_builder=_stub_builder, tfn_resolver=lambda ref: "111111111")
        await session.commit()
    assert len(lodge.calls) == 1, "must not re-lodge an ACCEPTED submission"
    assert result2.status == StpStatus.ACCEPTED.value
    assert result2.ato_receipt_number == "ATO-RECEIPT-123"


@pytest.mark.asyncio
async def test_submit_non_ready_state_no_double_submit() -> None:
    """(d) A SUBMITTED (in-flight) submission is not re-lodged."""
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id, status="SUBMITTED")
    lodge = _RecordingLodgement()
    async with AsyncSessionLocal() as session:
        with pytest.raises(stp_svc.StpError) as ei:
            await stp_svc.submit_event(
                session, sub_id, lodgement_service=lodge,
                document_builder=_stub_builder,
                tfn_resolver=lambda ref: "111111111")
    assert "state" in str(ei.value).lower() or "submitted" in str(ei.value).lower()
    assert lodge.calls == []


@pytest.mark.asyncio
async def test_tfn_plaintext_never_stored_or_logged(caplog) -> None:
    """(e) Decrypt seam is invoked; plaintext TFN never persisted/logged."""
    secret_tfn = "876543217"
    cid, _ = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id)
    seen = {}

    def _resolver(ref: str) -> str:
        seen["called_with"] = ref
        return secret_tfn

    captured_payloads = []

    def _builder(payload: dict) -> bytes:
        captured_payloads.append(payload)
        return b"<payevnt/>"

    lodge = _RecordingLodgement()
    with caplog.at_level("DEBUG"):
        async with AsyncSessionLocal() as session:
            await stp_svc.submit_event(
                session, sub_id, lodgement_service=lodge,
                document_builder=_builder, tfn_resolver=_resolver)
            await session.commit()

    # decrypt seam was used
    assert seen.get("called_with") == "see-secure-store"
    # plaintext never in any log record
    assert secret_tfn not in caplog.text
    # plaintext never written back to the stored submission
    sub = await _get_sub(sub_id)
    import json
    assert secret_tfn not in json.dumps(sub.payload)
    assert (sub.ato_response_payload is None
            or secret_tfn not in json.dumps(sub.ato_response_payload))
