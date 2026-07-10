"""Unit tests for services.journal.effective_audit_mode / enforce_posted_edit_gate.

Fast, service-level coverage of the fail-safe entitlement logic that
sits underneath both live callers (services.journal_entries.update and
the legacy services.journal.update_draft) — see
tests/api/v1/test_extended_audit_modes.py for the end-to-end contract
tests against the actually-reachable PATCH path.
"""
import uuid
from datetime import date

import pytest

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.services import journal as svc
from saebooks.services.journal import PostingError


async def _isolated_company(audit_mode: str) -> uuid.UUID:
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"AM-unit-{cid.hex[:6]}",
                audit_mode=audit_mode,
            )
        )
        await session.commit()
    return cid


# --------------------------------------------------------------------------- #
# effective_audit_mode — entitlement fail-safe                                #
# --------------------------------------------------------------------------- #


async def test_effective_mode_immutable_stored_stays_immutable() -> None:
    cid = await _isolated_company("immutable")
    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, cid, extended_audit_modes_entitled=True
        )
    assert mode == svc.AUDIT_MODE_IMMUTABLE


async def test_effective_mode_open_stored_and_entitled_is_open() -> None:
    cid = await _isolated_company("open")
    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, cid, extended_audit_modes_entitled=True
        )
    assert mode == svc.AUDIT_MODE_OPEN


async def test_effective_mode_open_stored_but_not_entitled_fails_safe() -> None:
    """The core fail-safe: a stored non-immutable value is IGNORED when
    the caller isn't entitled — this is what stops a below-tier install
    (or stale pre-Wave-C data) from getting open/hybrid behaviour."""
    cid = await _isolated_company("open")
    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, cid, extended_audit_modes_entitled=False
        )
    assert mode == svc.AUDIT_MODE_IMMUTABLE


async def test_effective_mode_hybrid_stored_but_not_entitled_fails_safe() -> None:
    cid = await _isolated_company("hybrid")
    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, cid, extended_audit_modes_entitled=False
        )
    assert mode == svc.AUDIT_MODE_IMMUTABLE


async def test_effective_mode_unknown_company_is_immutable() -> None:
    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, uuid.uuid4(), extended_audit_modes_entitled=True
        )
    assert mode == svc.AUDIT_MODE_IMMUTABLE


async def test_effective_mode_corrupt_stored_value_is_immutable() -> None:
    """A value outside {immutable, open, hybrid} (shouldn't happen post-
    0185, but defensive) fails safe rather than raising or defaulting
    to open."""
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(id=cid, tenant_id=DEFAULT_TENANT_ID, name="AM-corrupt")
        )
        await session.flush()
        # Bypass the ORM/validator to simulate genuinely corrupt data.
        await session.execute(
            Company.__table__.update()
            .where(Company.id == cid)
            .values(audit_mode="some-legacy-garbage")
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        mode = await svc.effective_audit_mode(
            session, cid, extended_audit_modes_entitled=True
        )
    assert mode == svc.AUDIT_MODE_IMMUTABLE


# --------------------------------------------------------------------------- #
# enforce_posted_edit_gate — draft entries always pass through unchecked      #
# --------------------------------------------------------------------------- #


async def test_enforce_gate_noop_for_draft_entry() -> None:
    cid = await _isolated_company("immutable")
    async with AsyncSessionLocal() as session:
        acct_a = Account(
            company_id=cid, tenant_id=DEFAULT_TENANT_ID, code="1-AMU",
            name="Asset", account_type=AccountType.ASSET, is_header=False,
        )
        acct_b = Account(
            company_id=cid, tenant_id=DEFAULT_TENANT_ID, code="6-AMU",
            name="Expense", account_type=AccountType.EXPENSE, is_header=False,
        )
        session.add_all([acct_a, acct_b])
        await session.flush()
        entry = await svc.create_draft(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 1),
            description="draft",
            lines=[
                {"account_id": acct_a.id, "debit": 10, "credit": 0},
                {"account_id": acct_b.id, "debit": 0, "credit": 10},
            ],
        )
        assert entry.status == EntryStatus.DRAFT
        # Must not raise even in immutable mode — drafts are always editable.
        await svc.enforce_posted_edit_gate(
            session, entry, extended_audit_modes_entitled=False
        )


# --------------------------------------------------------------------------- #
# update_draft — vocabulary + Setting-read bug fix (legacy path, still tested)#
# --------------------------------------------------------------------------- #


async def test_update_draft_immutable_blocks_posted_edit() -> None:
    """Regression guard for the fix itself: update_draft must read
    company.audit_mode (via effective_audit_mode), NOT the orphaned
    global Setting key it used before Wave C."""
    cid = await _isolated_company("immutable")
    async with AsyncSessionLocal() as session:
        acct_a = Account(
            company_id=cid, tenant_id=DEFAULT_TENANT_ID, code="1-AMU2",
            name="Asset", account_type=AccountType.ASSET, is_header=False,
        )
        acct_b = Account(
            company_id=cid, tenant_id=DEFAULT_TENANT_ID, code="6-AMU2",
            name="Expense", account_type=AccountType.EXPENSE, is_header=False,
        )
        session.add_all([acct_a, acct_b])
        await session.flush()
        entry = await svc.create_draft(
            session,
            company_id=cid,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=date(2026, 4, 1),
            description="pre-post",
            lines=[
                {"account_id": acct_a.id, "debit": 10, "credit": 0},
                {"account_id": acct_b.id, "debit": 0, "credit": 10},
            ],
        )
        posted = await svc.post(session, entry.id, posted_by="test")
        assert posted.status == EntryStatus.POSTED

    async with AsyncSessionLocal() as session:
        with pytest.raises(PostingError, match="immutable"):
            await svc.update_draft(
                session,
                entry.id,
                description="should be blocked",
                extended_audit_modes_entitled=False,
            )
