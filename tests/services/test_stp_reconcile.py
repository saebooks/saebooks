"""TDD coverage for the STP QUEUED->ACCEPTED reconcile poller.

This is the gate-independent follow-up to ``submit_event``: when the relay
returns a QUEUED (deferred) outcome, ``submit_event`` leaves the submission
in SUBMITTED state with the submission id as the server-side correlation
handle (``payevent_id`` == submission id; the relay also stashes whatever
``ato_receipt_id`` it had into ``ato_receipt_number``). A later poll resolves
the deferred receipt.

The REAL status retrieval (ATO ebMS3 response retrieval via the lodge-server
status route) does NOT exist yet — it is gated on the PVT pack. Here we inject
a deterministic test-double ``LodgementService`` with a ``poll_status`` method
so NO real ATO transmit ever happens. The transport/taxonomy specifics are NOT
exercised — only the engine orchestration + state machine.

Style mirrors tests/services/test_stp_submit_event.py: seed StpSubmission rows
directly via ORM under freshly created isolated companies.
"""
from __future__ import annotations

import uuid
from datetime import date as _date, datetime

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.pay_run import PayRun
from saebooks.models.stp_submission import StpStatus, StpSubmission
from saebooks.services.lodgement.base import LodgementResult, LodgementStatus
from saebooks.services.lodgement.exceptions import (
    LodgementRejected,
    LodgementUpstreamUnavailable,
)
from saebooks.services import stp as stp_svc


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #


class _PollingLodgement:
    """LodgementService double for the reconcile path.

    Records every ``poll_status`` call and returns a configured
    ``LodgementResult`` (default ACCEPTED). Set ``result`` to vary the
    outcome (ACCEPTED / QUEUED / STUB). To simulate an ATO rejection or an
    upstream failure, set ``raises`` to an exception instance.
    """

    def __init__(
        self,
        result: LodgementResult | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._raises = raises
        self._result = result or LodgementResult(
            status=LodgementStatus.ACCEPTED,
            ato_receipt_id="ATO-RECEIPT-POLLED-1",
            ato_timestamp=datetime(2026, 4, 12, 3, 4, 5),
            warnings=[],
            raw_response={"status": "accepted", "receipt": "ATO-RECEIPT-POLLED-1"},
        )

    async def poll_status(self, *, receipt_ref, product, metadata=None):
        self.calls.append(
            {"receipt_ref": receipt_ref, "product": product, "metadata": metadata}
        )
        if self._raises is not None:
            raise self._raises
        return self._result


# --------------------------------------------------------------------------- #
# Seeding helpers                                                             #
# --------------------------------------------------------------------------- #


async def _seed_company() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        c = Company(
            tenant_id=_DEFAULT_TENANT_ID,
            name="Acme",
            legal_name="Acme Pty Ltd",
            abn="12345678901",
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return c.id


async def _seed_pay_run(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        run = PayRun(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            period_start=_date(2026, 4, 1),
            period_end=_date(2026, 4, 7),
            payment_date=_date(2026, 4, 10),
            description="reconcile pytest run",
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
    status: str = "SUBMITTED",
    ato_receipt_number: str | None = None,
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        sub = StpSubmission(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            pay_run_id=pay_run_id,
            event_type="PAY",
            status=status,
            ato_receipt_number=ato_receipt_number,
            submitted_at=datetime(2026, 4, 10, 1, 2, 3) if status != "READY" else None,
            payload={
                "employer": {"abn": "12345678901", "legal_name": "Acme Pty Ltd",
                             "branch_code": "001"},
                "payees": [{"payee_id_bms": "EE1"}],
                "totals": {"gross": "1000.00", "tax": "200.00", "payee_count": 1},
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
# (a) SUBMITTED + poll->ACCEPTED  =>  ACCEPTED, receipt stored                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_queued_to_accepted() -> None:
    cid = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id, status="SUBMITTED")
    lodge = _PollingLodgement()
    async with AsyncSessionLocal() as session:
        result = await stp_svc.reconcile_stp_submission(
            session, sub_id, lodgement_service=lodge
        )
        await session.commit()
    assert len(lodge.calls) == 1
    # we poll by the submission id (the server-side payevent_id correlation)
    assert lodge.calls[0]["receipt_ref"] == str(sub_id)
    assert lodge.calls[0]["product"] == "stp"
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.ACCEPTED.value
    assert sub.ato_receipt_number == "ATO-RECEIPT-POLLED-1"
    assert result.status == StpStatus.ACCEPTED.value
    assert result.ato_receipt_number == "ATO-RECEIPT-POLLED-1"


# --------------------------------------------------------------------------- #
# (b) SUBMITTED + poll->REJECTED  =>  REJECTED, errors stored                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_poll_rejected() -> None:
    cid = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id, status="SUBMITTED")
    lodge = _PollingLodgement(
        raises=LodgementRejected(
            "ATO rejected on poll",
            ato_errors=[{"code": "CMN.ATO.GEN.XML05", "message": "Invalid ABN"}],
            raw_response={"status": "rejected"},
        )
    )
    async with AsyncSessionLocal() as session:
        result = await stp_svc.reconcile_stp_submission(
            session, sub_id, lodgement_service=lodge
        )
        await session.commit()
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.REJECTED.value
    assert sub.ato_receipt_number is None
    assert any(e.get("code") == "CMN.ATO.GEN.XML05" for e in sub.errors)
    assert result.status == StpStatus.REJECTED.value


# --------------------------------------------------------------------------- #
# (c) SUBMITTED + poll->QUEUED  =>  stays SUBMITTED (idempotent, re-pollable) #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_still_queued_stays_submitted() -> None:
    cid = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(cid, run_id, status="SUBMITTED")
    lodge = _PollingLodgement(
        result=LodgementResult(
            status=LodgementStatus.QUEUED,
            ato_receipt_id=None,
            ato_timestamp=None,
            warnings=[],
            raw_response={"status": "queued"},
        )
    )
    async with AsyncSessionLocal() as session:
        result = await stp_svc.reconcile_stp_submission(
            session, sub_id, lodgement_service=lodge
        )
        await session.commit()
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.SUBMITTED.value
    assert result.status == StpStatus.SUBMITTED.value
    # second poll is still allowed (re-pollable, idempotent no-op)
    async with AsyncSessionLocal() as session:
        result2 = await stp_svc.reconcile_stp_submission(
            session, sub_id, lodgement_service=lodge
        )
        await session.commit()
    assert result2.status == StpStatus.SUBMITTED.value
    assert len(lodge.calls) == 2


# --------------------------------------------------------------------------- #
# (e) terminal (already ACCEPTED) submission is NOT re-polled                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_terminal_not_repolled() -> None:
    cid = await _seed_company()
    run_id = await _seed_pay_run(cid)
    sub_id = await _seed_submission(
        cid, run_id, status="ACCEPTED", ato_receipt_number="ATO-PRIOR"
    )
    lodge = _PollingLodgement()
    async with AsyncSessionLocal() as session:
        result = await stp_svc.reconcile_stp_submission(
            session, sub_id, lodgement_service=lodge
        )
        await session.commit()
    # no poll happened — terminal state
    assert lodge.calls == []
    sub = await _get_sub(sub_id)
    assert sub.status == StpStatus.ACCEPTED.value
    assert sub.ato_receipt_number == "ATO-PRIOR"
    assert result.status == StpStatus.ACCEPTED.value


# --------------------------------------------------------------------------- #
# (d) reconcile_pending over a mix — one resolves, one stays, one raises      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_pending_mixed_batch() -> None:
    cid = await _seed_company()
    run_id = await _seed_pay_run(cid)
    # three SUBMITTED submissions under the same company
    sub_accept = await _seed_submission(cid, run_id, status="SUBMITTED")
    sub_queued = await _seed_submission(cid, run_id, status="SUBMITTED")
    sub_fails = await _seed_submission(cid, run_id, status="SUBMITTED")

    class _PerSubLodgement:
        """Route the poll outcome by the receipt_ref (submission id)."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def poll_status(self, *, receipt_ref, product, metadata=None):
            self.calls.append(receipt_ref)
            if receipt_ref == str(sub_accept):
                return LodgementResult(
                    status=LodgementStatus.ACCEPTED,
                    ato_receipt_id="ATO-BATCH-OK",
                    ato_timestamp=datetime(2026, 4, 12, 0, 0, 0),
                    warnings=[],
                    raw_response={"status": "accepted"},
                )
            if receipt_ref == str(sub_queued):
                return LodgementResult(
                    status=LodgementStatus.QUEUED,
                    ato_receipt_id=None,
                    ato_timestamp=None,
                    warnings=[],
                    raw_response={"status": "queued"},
                )
            # sub_fails — transient upstream error
            raise LodgementUpstreamUnavailable(
                status=502, detail="ATO SBR endpoint unreachable"
            )

    lodge = _PerSubLodgement()
    async with AsyncSessionLocal() as session:
        results = await stp_svc.reconcile_pending_stp(
            session, lodgement_service=lodge, company_id=cid
        )
        await session.commit()

    # batch completed: all three were attempted, none aborted the batch
    assert len(lodge.calls) == 3
    assert len(results) == 3

    accept = await _get_sub(sub_accept)
    queued = await _get_sub(sub_queued)
    fails = await _get_sub(sub_fails)
    assert accept.status == StpStatus.ACCEPTED.value
    assert accept.ato_receipt_number == "ATO-BATCH-OK"
    assert queued.status == StpStatus.SUBMITTED.value  # still queued
    assert fails.status == StpStatus.SUBMITTED.value   # raiser left in-flight

    by_id = {r.submission_id: r for r in results}
    assert by_id[sub_accept].status == StpStatus.ACCEPTED.value
    assert by_id[sub_queued].status == StpStatus.SUBMITTED.value
    assert by_id[sub_fails].status == StpStatus.SUBMITTED.value
    # the raiser records the transient error on its per-item result
    assert by_id[sub_fails].errors, "transient poll failure surfaced per-item"


@pytest.mark.asyncio
async def test_reconcile_not_found() -> None:
    lodge = _PollingLodgement()
    async with AsyncSessionLocal() as session:
        with pytest.raises(stp_svc.StpError) as ei:
            await stp_svc.reconcile_stp_submission(
                session, uuid.uuid4(), lodgement_service=lodge
            )
    assert ei.value.code == "not_found"
