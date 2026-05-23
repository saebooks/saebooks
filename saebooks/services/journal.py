"""Journal entry service — create, update, post, reverse, delete.

Business rules:
- Drafts may be unbalanced; posts must balance (sum debits == sum credits).
- Period-lock check: posting into a locked period requires override_reason.
- Immutable mode (Community default): posted entries can only be reversed,
  not edited. Hybrid/Open modes allow edit with full audit trail.
- Auto-ref: JE-NNNNNN, from a Postgres sequence. User may override.
"""
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, PeriodLock
from saebooks.services import audit as audit_svc
from saebooks.services import gst as gst_svc
from saebooks.services import settings as settings_svc


class PostingError(Exception):
    pass


async def _validate_line_accounts(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[dict[str, object]],
    *,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Raise PostingError if any line account belongs to a different company or tenant."""
    if not lines:
        return
    ids = [uuid.UUID(str(ln["account_id"])) for ln in lines]
    result = await session.execute(
        select(Account.id, Account.company_id, Account.tenant_id).where(Account.id.in_(ids))
    )
    rows = {r.id: r for r in result.all()}
    bad = []
    for i in ids:
        row = rows.get(i)
        if row is None or row.company_id != company_id:
            bad.append(i)
        elif tenant_id is not None and row.tenant_id != tenant_id:
            bad.append(i)
    if bad:
        raise PostingError(
            "Account(s) do not belong to this company: "
            + ", ".join(str(i) for i in bad)
        )


async def next_ref(session: AsyncSession) -> str:
    result = await session.execute(text("SELECT nextval('journal_ref_seq')"))
    seq = result.scalar_one()
    return f"JE-{seq:06d}"


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entry_date: date,
    description: str | None = None,
    ref: str | None = None,
    lines: list[dict[str, object]] | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    if not ref:
        ref = await next_ref(session)

    if len(ref) > 32:
        raise PostingError(
            f"Reference must be 32 characters or less (you provided {len(ref)})"
        )

    entry = JournalEntry(
        company_id=company_id,
        ref=ref,
        entry_date=entry_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    if lines:
        await _validate_line_accounts(session, company_id, lines, tenant_id=tenant_id)
        for i, line_data in enumerate(lines, 1):
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    line_no=i,
                    account_id=line_data["account_id"],
                    description=line_data.get("description"),
                    debit=Decimal(str(line_data.get("debit", 0))),
                    credit=Decimal(str(line_data.get("credit", 0))),
                    tax_code_id=line_data.get("tax_code_id"),
                    gst_amount=line_data.get("gst_amount"),
                    project_id=line_data.get("project_id"),
                    department_id=line_data.get("department_id"),
                    cost_centre_id=line_data.get("cost_centre_id"),
                )
            )

    await session.commit()
    return await get(session, entry.id)


async def get(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Fetch a journal entry by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id raises ``ValueError`` (treated as not found),
    so cross-tenant probes 404 even if the underlying row exists.
    Belt-and-braces complement to FORCE RLS at the DB layer.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.id == entry_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(JournalEntry.tenant_id == tenant_id)
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry is None:
        raise ValueError(f"Journal entry {entry_id} not found")
    return entry


async def list_entries(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: EntryStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[JournalEntry]:
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.company_id == company_id)
    )
    if status is not None:
        stmt = stmt.where(JournalEntry.status == status)
    stmt = stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.ref.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    entry_date: date | None = None,
    description: str | None = None,
    ref: str | None = None,
    lines: list[dict[str, object]] | None = None,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if entry.status != EntryStatus.DRAFT:
        audit_mode = await settings_svc.get(session, "audit_mode", "immutable")
        if audit_mode == "immutable":
            raise PostingError("Cannot edit a posted entry in immutable mode — reverse instead")

    before = audit_svc.capture(entry)

    if entry_date is not None:
        entry.entry_date = entry_date
    if description is not None:
        entry.description = description
    if ref is not None:
        if len(ref) > 32:
            raise PostingError(
                f"Reference must be 32 characters or less (you provided {len(ref)})"
            )
        entry.ref = ref

    if lines is not None:
        await _validate_line_accounts(session, entry.company_id, lines, tenant_id=entry.tenant_id)
        # Replace all lines
        for old_line in entry.lines:
            await session.delete(old_line)
        await session.flush()
        for i, line_data in enumerate(lines, 1):
            session.add(
                JournalLine(
                    entry_id=entry.id,
                    line_no=i,
                    account_id=line_data["account_id"],
                    description=line_data.get("description"),
                    debit=Decimal(str(line_data.get("debit", 0))),
                    credit=Decimal(str(line_data.get("credit", 0))),
                    tax_code_id=line_data.get("tax_code_id"),
                    gst_amount=line_data.get("gst_amount"),
                    project_id=line_data.get("project_id"),
                    department_id=line_data.get("department_id"),
                    cost_centre_id=line_data.get("cost_centre_id"),
                )
            )

    await audit_svc.snapshot_row(
        session, entry,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    return await get(session, entry_id, tenant_id=tenant_id)


async def _check_balance(entry: JournalEntry) -> None:
    total_debit = sum(line.debit for line in entry.lines)
    total_credit = sum(line.credit for line in entry.lines)
    if total_debit != total_credit:
        raise PostingError(
            f"Entry {entry.ref} is unbalanced: "
            f"debits={total_debit}, credits={total_credit}"
        )
    if not entry.lines:
        raise PostingError(f"Entry {entry.ref} has no lines")


async def _check_trust_commingling(
    session: AsyncSession,
    entry: JournalEntry,
) -> None:
    """Raise PostingError for either of two trust-violation patterns.

    Pattern 1 (RLES-1): the JE moves funds between trust and non-trust bank
    accounts. Under NSW Property and Stock Agents Act 2002, trust funds must
    be kept strictly segregated from operating funds. A JE that debits an
    operating bank account and credits a trust bank account (or vice versa)
    is a commingling breach.

    Pattern 2 (RLES-2): the JE debits a trust bank account and credits a
    revenue/income account. Rent collected on behalf of landlords is trust
    money — it is never the agency's income. The credit must go to a trust
    liability account (e.g. Landlord / Owner Trust Liability). This pattern
    inflates BAS G1 because the revenue account flows into GST turnover
    while the actual agency revenue is only the management fee.

    Only reconcilable ASSET accounts (bank accounts) are examined for bank
    classification; income and expense account types are checked by
    account_type for Pattern 2.
    """
    if not entry.lines:
        return

    account_ids = [line.account_id for line in entry.lines]
    result = await session.execute(
        select(Account.id, Account.is_trust_account, Account.reconcile, Account.account_type).where(
            Account.id.in_(account_ids)
        )
    )
    rows = {r.id: (r.is_trust_account, r.reconcile, r.account_type) for r in result.all()}

    # Pattern 1: trust bank ↔ operating bank transfer.
    trust_banks = [aid for aid, (is_trust, reconcile, _t) in rows.items() if reconcile and is_trust]
    non_trust_banks = [aid for aid, (is_trust, reconcile, _t) in rows.items() if reconcile and not is_trust]

    if trust_banks and non_trust_banks:
        raise PostingError(
            "This journal entry moves funds between a trust bank account and an operating "
            "bank account. Commingling trust funds with operating funds is prohibited under "
            "the NSW Property and Stock Agents Act 2002. Use a trust disbursement workflow "
            "to transfer funds between trust and operating accounts."
        )

    # Pattern 2: trust bank debited + revenue/income account credited.
    # Trust money received (e.g. rent) must credit a trust liability, not revenue.
    trust_bank_debited = any(
        line.debit > Decimal("0")
        for line in entry.lines
        if rows.get(line.account_id, (False, False, None))[0]
        and rows.get(line.account_id, (False, False, None))[1]
    )
    if trust_bank_debited:
        income_credited = any(
            line.credit > Decimal("0")
            for line in entry.lines
            if rows.get(line.account_id, (False, False, None))[2]
            in (AccountType.INCOME, AccountType.OTHER_INCOME)
        )
        if income_credited:
            raise PostingError(
                "This journal entry debits a trust bank account and credits a revenue "
                "account. Rent and other funds collected in trust on behalf of clients "
                "are not agency income — the credit must go to a trust liability account "
                "(e.g. Landlord / Owner Trust Liability, account 2-1780). Only management "
                "fees and commissions earned by the agency should credit revenue accounts. "
                "See gap RLES-2."
            )


# ITAA97 s.86-70 — wages paid to related parties under a PSI arrangement
# are non-deductible. Flag JEs that debit a wages account (6-243x range)
# and mention a related-party payee in any description field.
_PSI_WAGES_PREFIX = "6-243"
_PSI_RELATED_PATTERN = re.compile(
    r"\b(spouse|partner|family|related[- ]party|non[- ]arms[- ]length)\b",
    re.IGNORECASE,
)


async def _check_psi_distribution(
    session: AsyncSession,
    entry: JournalEntry,
    override_reason: str | None,
) -> None:
    """Raise PostingError for wages paid to a related party without override.

    Triggers when a JE debits a wages account (6-243x) and any description
    field contains a related-party indicator (spouse, partner, family, etc.).
    Passing override_reason records compliance acknowledgement and allows post.
    """
    if not entry.lines:
        return

    debited_ids = [line.account_id for line in entry.lines if line.debit > Decimal("0")]
    if not debited_ids:
        return

    result = await session.execute(
        select(Account.id, Account.code, Account.account_type).where(
            Account.id.in_(debited_ids)
        )
    )
    rows = {r.id: (r.code, r.account_type) for r in result.all()}

    wages_debited = any(
        code.startswith(_PSI_WAGES_PREFIX) and acct_type == AccountType.EXPENSE
        for _id, (code, acct_type) in rows.items()
    )
    if not wages_debited:
        return

    texts: list[str] = []
    if entry.description:
        texts.append(entry.description)
    for line in entry.lines:
        if line.description:
            texts.append(line.description)

    if not any(_PSI_RELATED_PATTERN.search(t) for t in texts):
        return

    if not override_reason:
        raise PostingError(
            "Non-arms-length wage payment flagged for PSI review: "
            "s.86-70 ITAA97 restricts deductibility of distributions to related parties. "
            "Provide a compliance override reason (e.g. 'Business determination in place' "
            "or 'PAYG-W withholding applied') to post this entry. (gap PSI-3)"
        )


async def _check_period_lock(
    session: AsyncSession,
    company_id: uuid.UUID,
    entry_date: date,
    override_reason: str | None,
) -> None:
    result = await session.execute(
        select(func.max(PeriodLock.locked_through)).where(
            PeriodLock.company_id == company_id
        )
    )
    locked_through = result.scalar_one_or_none()
    if locked_through is not None and entry_date <= locked_through:
        if not override_reason:
            raise PostingError(
                f"Period is locked through {locked_through}. "
                f"Provide an override reason to post into a locked period."
            )
        # F-04 interim: refuse trivial / stop-word overrides. A non-admin
        # user can still bypass by typing a 12-char string, but the worst
        # case ("x" / "override" / blank-ish) is closed off. Full
        # role-gate plumbing (actor_role through post/reverse/update_draft
        # and bank_feeds.py:464) is tracked separately — see overnight
        # 2026-05-13/F-04-deferred.md.
        _cleaned = override_reason.strip().lower()
        _STOPS = {"x", ".", "yes", "ok", "override", "reason", "no", "y", "-", "na", "n/a"}
        if len(_cleaned) < 12 or _cleaned in _STOPS:
            raise PostingError(
                f"Period is locked through {locked_through}. "
                f"Override reason must be a meaningful explanation "
                f"(minimum 12 characters, not a stop-word). "
                f"Provided: {override_reason!r}."
            )


async def post(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if entry.status == EntryStatus.POSTED:
        raise PostingError(f"Entry {entry.ref} is already posted")
    if entry.status == EntryStatus.REVERSED:
        raise PostingError(f"Entry {entry.ref} has been reversed")

    await _check_period_lock(session, entry.company_id, entry.entry_date, override_reason)

    # Trust commingling guard — must run BEFORE GST auto-posting so GST lines
    # can't mask a trust→operating transfer that was originally balanced without them.
    await _check_trust_commingling(session, entry)

    # PSI distribution guard — ITAA97 s.86-70 related-party wages check.
    await _check_psi_distribution(session, entry, override_reason)

    # Auto-generate GST account lines BEFORE balancing.
    # Lines may carry `gst_amount` as the net/gross split metadata —
    # the auto-poster adds the matching DR GST Paid / CR GST Collected
    # line so the entry balances. Pre-checking balance here would reject
    # legitimate net+gst entries (e.g. DR Telephone 100 [+gst 10] / CR Bank 110).
    gst_lines = await gst_svc.auto_post_gst_lines(session, entry)
    if gst_lines:
        await session.flush()
        # auto_post_gst_lines appends to entry.lines in-place, so no re-fetch needed.

    # Final balance check — the entry must balance after GST has been posted.
    await _check_balance(entry)

    entry.status = EntryStatus.POSTED
    entry.posted_at = datetime.now(UTC)
    entry.posted_by = posted_by
    if override_reason:
        entry.override_reason = override_reason

    await session.commit()
    return entry


async def reverse(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    reversal_date: date | None = None,
    posted_by: str | None = None,
    override_reason: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Create and post a reversal of a posted entry."""
    original = await get(session, entry_id, tenant_id=tenant_id)
    if original.status != EntryStatus.POSTED:
        raise PostingError(f"Can only reverse posted entries (current: {original.status})")

    # Snapshot the original before we flip its status to REVERSED.
    await audit_svc.snapshot_row(
        session, original,
        action="reverse",
        reason=f"Reversed by new entry (date={reversal_date or original.entry_date})",
        performed_by=posted_by,
    )

    rev_date = reversal_date or original.entry_date
    rev_ref = await next_ref(session)

    reversal = JournalEntry(
        company_id=original.company_id,
        ref=rev_ref,
        entry_date=rev_date,
        description=f"Reversal of {original.ref}: {original.description or ''}".strip(),
        status=EntryStatus.DRAFT,
        reversal_of_id=original.id,
    )
    session.add(reversal)
    await session.flush()

    for line in original.lines:
        session.add(
            JournalLine(
                entry_id=reversal.id,
                line_no=line.line_no,
                account_id=line.account_id,
                description=line.description,
                debit=line.credit,
                credit=line.debit,
                tax_code_id=line.tax_code_id,
                # Do NOT carry gst_amount onto the reversal — the original
                # GST Collected/Paid line has already been copied (with
                # debit/credit swapped). Passing gst_amount would cause
                # auto_post_gst_lines to add a duplicate GST line on post().
                gst_amount=None,
                project_id=line.project_id,
                department_id=line.department_id,
                cost_centre_id=line.cost_centre_id,
            )
        )

    await session.commit()

    # Auto-post the reversal
    reversal = await post(
        session, reversal.id, posted_by=posted_by, override_reason=override_reason
    )

    # Mark original as reversed
    original.status = EntryStatus.REVERSED
    await session.commit()

    return reversal


async def delete(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Delete a journal entry and its lines. Any status — MYOB-style.

    Audit M5 guarantee: BEFORE the SQLAlchemy cascade nukes the line
    rows, snapshot each one to audit_snapshots so the GL detail
    survives the delete. Without this loop only the header is
    captured and the line-level meaning is lost.
    """
    entry = await get(session, entry_id, tenant_id=tenant_id)
    await audit_svc.snapshot_row(
        session, entry,
        action="delete",
        performed_by=performed_by,
        reason=f"cascade-parent-of journal_entry {entry.id}",
    )
    for line in list(entry.lines):
        await audit_svc.snapshot_row(
            session, line,
            action="delete",
            performed_by=performed_by,
            reason=f"cascade-from journal_entry {entry.id}",
        )
    await session.delete(entry)
    await session.commit()


async def lock_period(
    session: AsyncSession,
    company_id: uuid.UUID,
    locked_through: date,
    *,
    locked_by: str | None = None,
    reason: str | None = None,
) -> PeriodLock:
    lock = PeriodLock(
        company_id=company_id,
        locked_through=locked_through,
        locked_by=locked_by,
        reason=reason,
    )
    session.add(lock)
    await session.commit()
    return lock


async def get_locked_through(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> date | None:
    """Return the most recent period-lock end date for ``company_id``, or None."""
    result = await session.execute(
        select(func.max(PeriodLock.locked_through)).where(
            PeriodLock.company_id == company_id
        )
    )
    return result.scalar_one_or_none()
