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

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
    PeriodLock,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import audit as audit_svc
from saebooks.services import audit_log as audit_log_svc
from saebooks.services import gst as gst_svc
from saebooks.services import settings as settings_svc
from saebooks.services.tax_engine import get_engine
from saebooks.services.tax_engine.types import PostingContext


class PostingError(Exception):
    pass


# F-04: roles allowed to override a closed period-lock. Bookkeepers and
# viewers cannot bypass a period lock regardless of the reason they type;
# they must hand off to an accountant or admin. Owners outrank admins in
# the role hierarchy so they are included here too — excluding them would
# leave a tenant's primary owner unable to override their own lock.
_OVERRIDE_ROLES: frozenset[str] = frozenset({"admin", "accountant", "owner"})


async def _validate_line_accounts(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[dict[str, object]],
    *,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Raise PostingError if any line account belongs to a different
    company / tenant, or is a header (group) account.

    Header accounts are CoA scaffolding — they can carry sub-accounts but
    are not themselves postable. A JE that lands on a header silently
    skews the balance roll-up because reports aggregate leaves only.
    """
    if not lines:
        return
    ids = [uuid.UUID(str(ln["account_id"])) for ln in lines]
    result = await session.execute(
        select(Account.id, Account.company_id, Account.tenant_id, Account.is_header)
        .where(Account.id.in_(ids))
    )
    rows = {r.id: r for r in result.all()}
    bad_company: list[uuid.UUID] = []
    bad_header: list[uuid.UUID] = []
    for i in ids:
        row = rows.get(i)
        if row is None or row.company_id != company_id or (tenant_id is not None and row.tenant_id != tenant_id):
            bad_company.append(i)
        elif row.is_header:
            bad_header.append(i)
    if bad_company:
        raise PostingError(
            "Account(s) do not belong to this company: "
            + ", ".join(str(i) for i in bad_company)
        )
    if bad_header:
        raise PostingError(
            "Cannot post to header (group) account(s) — these are CoA "
            "scaffolding for sub-accounts and must not carry their own "
            "journal lines: " + ", ".join(str(i) for i in bad_header)
        )


async def next_ref(session: AsyncSession) -> str:
    """Return the next ``JE-NNNNNN`` reference for a draft entry.

    Postgres path uses the ``journal_ref_seq`` SEQUENCE (migration 0005)
    so concurrent callers don''t collide. SQLite has no SEQUENCE
    primitive, so we fall back to MAX(extracted-number)+1 — safe on
    Cashbook because there''s exactly one writer (the local app on
    the device) and journal entries are append-only from a single
    session.
    """
    bind = session.bind
    if bind is not None and bind.dialect.name != "postgresql":
        result = await session.execute(
            text(
                "SELECT COALESCE(MAX(CAST(SUBSTR(ref, 4) AS INTEGER)), 0) + 1 "
                "FROM journal_entries WHERE ref LIKE 'JE-%'"
            )
        )
        seq = int(result.scalar_one() or 1)
        return f"JE-{seq:06d}"
    result = await session.execute(text("SELECT nextval('journal_ref_seq')"))
    seq = result.scalar_one()
    return f"JE-{seq:06d}"


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    entry_date: date,
    tenant_id: uuid.UUID,
    description: str | None = None,
    ref: str | None = None,
    lines: list[dict[str, object]] | None = None,
) -> JournalEntry:
    # tenant_id is REQUIRED. Without it the JournalEntry server-default
    # (00000000-0000-0000-0000-000000000001, the legacy dev tenant) fires
    # and the row lands invisible to any tenant-scoped reader. The
    # superuser DATABASE_URL bypasses the RLS WITH CHECK that would
    # otherwise catch this at insert time, so the only thing standing
    # between callers and a silent cross-tenant leak is this argument.
    if tenant_id is None:  # caller passed None explicitly
        raise PostingError(
            "create_draft requires tenant_id — never let it default to "
            "the legacy 00000000 tenant. Resolve it from the request via "
            "saebooks.routers.deps.resolve_tenant_id and thread it through."
        )
    if not ref:
        ref = await next_ref(session)

    if len(ref) > 32:
        raise PostingError(
            f"Reference must be 32 characters or less (you provided {len(ref)})"
        )

    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
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
    actor_role: str | None = None,
) -> None:
    """Block posts into a locked period unless an authorised actor overrides.

    Override path requires BOTH:
      1. ``actor_role`` is one of ``_OVERRIDE_ROLES`` (admin / accountant /
         owner). A bookkeeper or viewer is rejected regardless of what they
         type in ``override_reason``. Missing role is treated as not-allowed
         (fail-closed) — callers that genuinely have no user context (system
         flows like deferred-revenue auto-recognition) should pass an
         explicit privileged role string after authenticating the request,
         or land their entry in an open period.
      2. ``override_reason`` is a non-trivial human-readable explanation:
         at least 12 characters after trim/lowercase, and not in the
         stop-word set. This is the F-04 interim guard kept as a
         second-line check; it still helps an auditor spot drive-by overrides
         even from an authorised actor.

    Raises ``PostingError`` for either failure with a message describing the
    failure mode (role vs reason).
    """
    result = await session.execute(
        select(func.max(PeriodLock.locked_through)).where(
            PeriodLock.company_id == company_id
        )
    )
    locked_through = result.scalar_one_or_none()
    if locked_through is None or entry_date > locked_through:
        return

    if not override_reason:
        raise PostingError(
            f"Period is locked through {locked_through}. "
            f"Provide an override reason to post into a locked period."
        )

    # First-line gate (F-04 full fix): role must be admin / accountant /
    # owner. Reject before the reason-content check so a stop-word reason
    # from an unprivileged actor gets the role-gate error (more useful to
    # the operator: "I'm not allowed", not "my reason is too short").
    if actor_role not in _OVERRIDE_ROLES:
        raise PostingError(
            f"Period is locked through {locked_through}. "
            f"Override requires admin or accountant role "
            f"(your role: {actor_role or 'unknown'})."
        )

    # Second-line gate (F-04 interim, retained): refuse trivial /
    # stop-word overrides even from authorised actors. A real explanation
    # in the audit trail is much more useful to an auditor than "ok".
    _cleaned = override_reason.strip().lower()
    _STOPS = {"x", ".", "yes", "ok", "override", "reason", "no", "y", "-", "na", "n/a"}
    if len(_cleaned) < 12 or _cleaned in _STOPS:
        raise PostingError(
            f"Period is locked through {locked_through}. "
            f"Override reason must be a meaningful explanation "
            f"(minimum 12 characters, not a stop-word). "
            f"Provided: {override_reason!r}."
        )


async def _apply_tax_treatment(
    session: AsyncSession, entry: JournalEntry
) -> None:
    """Snapshot a TaxTreatment onto every line via the AU tax engine.

    M0 ships AU only; the per-jurisdiction dispatcher will be plumbed
    in once company.jurisdiction lands. Until then every post is AU.

    Runs AFTER ``gst_svc.auto_post_gst_lines`` so the auto-added GST
    Collected/Paid line gets its own snapshot too (direction='none'
    because GST liability/asset accounts aren't in the input/output
    sets — they're plumbing, not BAS-reportable themselves).
    """
    engine = get_engine("AU")
    # Pre-load account types + tax-code attrs for every distinct id on
    # the entry — avoids an N+1 lookup per line.
    acct_ids = {ln.account_id for ln in entry.lines}
    tc_ids = {ln.tax_code_id for ln in entry.lines if ln.tax_code_id is not None}
    acct_rows = (
        (
            await session.execute(
                select(Account.id, Account.account_type).where(
                    Account.id.in_(acct_ids)
                )
            )
        ).all()
        if acct_ids
        else []
    )
    acct_type = {row[0]: row[1] for row in acct_rows}
    tc_rows = (
        (
            await session.execute(
                select(
                    TaxCode.id, TaxCode.code, TaxCode.rate, TaxCode.reporting_type
                ).where(TaxCode.id.in_(tc_ids))
            )
        ).all()
        if tc_ids
        else []
    )
    tc_meta = {row[0]: (row[1], row[2], row[3]) for row in tc_rows}

    for ln in entry.lines:
        # debit + credit captures whichever side is non-zero (exactly
        # one is > 0 on a balanced line) — gives the line amount
        # without branching on direction.
        amount = (ln.debit or Decimal("0")) + (ln.credit or Decimal("0"))
        tc_code, tc_rate, tc_reporting = (None, None, None)
        if ln.tax_code_id is not None and ln.tax_code_id in tc_meta:
            tc_code, tc_rate, tc_reporting = tc_meta[ln.tax_code_id]
        ctx = PostingContext(
            company_id=entry.company_id,
            jurisdiction="AU",
            posting_date=entry.entry_date,
            account_id=ln.account_id,
            account_type=acct_type.get(ln.account_id, AccountType.ASSET),
            amount=amount,
            gst_amount=ln.gst_amount,
            tax_code=tc_code,
            tax_code_id=ln.tax_code_id,
            rate=tc_rate,
            reporting_type=tc_reporting,
        )
        treatment = engine.compute(ctx)
        ln.tax_treatment = treatment.to_jsonable()


async def post_in_txn(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    origin: JournalOrigin = JournalOrigin.MANUAL,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Transition DRAFT → POSTED WITHOUT committing — caller owns the txn.

    Composable primitive the intercompany engine wraps so two entries (one
    per company) can post in a single transaction. ``post()`` is the thin
    commit-here wrapper that every existing single-entry caller uses, so
    their behaviour is unchanged.

    All posting guards run here exactly as before: balance validation, the
    period-lock override gate, trust-commingling + PSI distribution guards,
    GST auto-posting, per-line tax-treatment snapshots, and the C2
    override-post audit-log row. The only difference vs the old ``post()``
    body is the trailing ``await session.flush()`` in place of
    ``await session.commit()`` — the wrapper commits.

    ``actor_role`` is the role string of the user driving the post — used
    only by the period-lock override gate (see ``_check_period_lock``).
    Callers that don't pass it cannot override a locked period. The
    role is also recorded on ``entry.override_reason`` for audit when an
    override is accepted.

    ``origin`` / ``source_type`` / ``source_id`` are the JE-provenance
    keystone (migration 0153). ``origin`` defaults to
    ``JournalOrigin.MANUAL`` — any caller that does not declare a machine
    origin is flagged as a manual/arbitrary entry, the visible exception.
    Auto-posting services pass their real origin plus the originating
    record's ``source_type`` (e.g. ``"invoice"``) and ``source_id`` so each
    posted entry self-declares what created it. Stamped only on a DRAFT →
    POSTED transition; re-posting is impossible (guarded above), so a
    posted entry's provenance is write-once.
    """
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if entry.status == EntryStatus.POSTED:
        raise PostingError(f"Entry {entry.ref} is already posted")
    if entry.status == EntryStatus.REVERSED:
        raise PostingError(f"Entry {entry.ref} has been reversed")

    await _check_period_lock(
        session, entry.company_id, entry.entry_date, override_reason, actor_role
    )

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

    # Snapshot per-line tax determination onto journal_lines.tax_treatment.
    # Runs after auto_post_gst_lines so the auto-added GST line also gets
    # a treatment row (direction='none' for GST liability/asset accounts).
    await _apply_tax_treatment(session, entry)

    # Final balance check — the entry must balance after GST has been posted.
    await _check_balance(entry)

    entry.status = EntryStatus.POSTED
    entry.posted_at = datetime.now(UTC)
    entry.posted_by = posted_by
    # JE-provenance keystone: stamp what created this entry. Default
    # origin=MANUAL (set in the signature) so a bare manual/API post is the
    # visible exception; auto-posting services pass their real origin + the
    # originating record's source_type/source_id.
    entry.origin = origin
    entry.source_type = source_type
    entry.source_id = source_id
    if override_reason:
        entry.override_reason = override_reason

    # C2 audit: a post into a LOCKED period via an authorised override is a
    # compliance-relevant event. _check_period_lock raised above for an
    # unauthorised/reason-less override, so reaching here with an
    # override_reason means the override was accepted. The audit row is
    # staged on this same session and commits with the post below; if any
    # later step were to raise, it rolls back with the post (no orphan).
    if override_reason and actor_user_id is not None:
        snapshot = jsonable_encoder(
            {c.key: getattr(entry, c.key) for c in entry.__table__.columns}
        )
        await audit_log_svc.append(
            session,
            tenant_id=entry.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.JOURNAL_OVERRIDE_POST,
            table_name="journal_entries",
            row_id=str(entry.id),
            row_snapshot=snapshot,
            reason=override_reason,
        )

    await session.flush()
    return entry


async def post(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    origin: JournalOrigin = JournalOrigin.MANUAL,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Transition DRAFT → POSTED and commit.

    Thin wrapper over :func:`post_in_txn` — behaviour is unchanged for every
    existing single-entry caller (it flushes-then-commits, the same net effect
    as the previous mutate-then-commit body). ``origin`` / ``source_type`` /
    ``source_id`` are the JE-provenance keystone passed straight through (see
    :func:`post_in_txn`); default ``origin=MANUAL``.
    """
    entry = await post_in_txn(
        session,
        entry_id,
        posted_by=posted_by,
        override_reason=override_reason,
        actor_role=actor_role,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        origin=origin,
        source_type=source_type,
        source_id=source_id,
    )
    await session.commit()
    return entry


async def reverse(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    reversal_date: date | None = None,
    posted_by: str | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Create and post a reversal of a posted entry.

    ``actor_role`` is threaded through to ``post()`` so the period-lock
    override gate also applies to reversals that land in a closed period
    (the reversal entry's date may be the original's entry_date which is
    by definition in or before the lock).
    """
    original = await get(session, entry_id, tenant_id=tenant_id)
    if original.status != EntryStatus.POSTED:
        raise PostingError(f"Can only reverse posted entries (current: {original.status})")

    # Snapshot the original before we flip its status to REVERSED.
    # actor_role is included in the audit reason so an auditor can see
    # which authorisation level approved the reversal (F-04).
    _reason = f"Reversed by new entry (date={reversal_date or original.entry_date})"
    if actor_role:
        _reason = f"{_reason}; actor_role={actor_role}"
    await audit_svc.snapshot_row(
        session, original,
        action="reverse",
        reason=_reason,
        performed_by=posted_by,
    )

    rev_date = reversal_date or original.entry_date
    rev_ref = await next_ref(session)

    reversal = JournalEntry(
        company_id=original.company_id,
        # Inherit tenant from the original entry — a reversal logically
        # belongs to the same tenant. Don't trust the request tenant_id
        # here: ``get()`` above already enforces tenant access to the
        # original, and using original.tenant_id keeps the pair atomic
        # if an admin in a different tenant ever drives a reversal.
        tenant_id=original.tenant_id,
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

    # Auto-post the reversal — pass actor_role through so the period-lock
    # override gate fires consistently for the reversal post. Provenance:
    # origin=REVERSAL, source = the original journal entry being reversed.
    reversal = await post(
        session,
        reversal.id,
        posted_by=posted_by,
        override_reason=override_reason,
        actor_role=actor_role,
        origin=JournalOrigin.REVERSAL,
        source_type="journal_entry",
        source_id=original.id,
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
    company_id: uuid.UUID | None = None,
) -> None:
    """Delete a DRAFT journal entry and its lines.

    Phase 0 hardening: a POSTED or REVERSED entry must be REVERSED, never
    hard-deleted (intercompany-linked entries must survive so the paired
    leg can never be orphaned). When ``company_id`` is supplied, the entry
    must belong to it — a cross-company delete is refused. Both guards raise
    :class:`PostingError`. Passing ``company_id=None`` preserves the legacy
    callers' behaviour for DRAFT entries.

    Audit M5 guarantee: BEFORE the SQLAlchemy cascade nukes the line
    rows, snapshot each one to audit_snapshots so the GL detail
    survives the delete. Without this loop only the header is
    captured and the line-level meaning is lost.
    """
    entry = await get(session, entry_id, tenant_id=tenant_id)
    if company_id is not None and entry.company_id != company_id:
        raise PostingError(
            f"Entry {entry.ref} does not belong to this company"
        )
    if entry.status in (EntryStatus.POSTED, EntryStatus.REVERSED):
        raise PostingError(
            f"Entry {entry.ref} is {entry.status}; reverse it instead of "
            "deleting (intercompany-linked entries must never be hard-deleted)"
        )
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
