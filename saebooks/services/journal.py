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
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import (
    EntryStatus,
    JournalEntry,
    JournalLine,
    JournalOrigin,
    PeriodLock,
)
from saebooks.models.journal_line_tax_component import JournalLineTaxComponent
from saebooks.models.tax_code import TaxCode
from saebooks.services import audit as audit_svc
from saebooks.services import audit_log as audit_log_svc
from saebooks.services import features as features_svc
from saebooks.services import gst as gst_svc
from saebooks.services.tax_engine import get_engine
from saebooks.services.tax_engine.types import PostingContext, PostingError

# PostingError is defined in the leaf ``tax_engine.types`` module so the
# tax engine can subclass it (TaxConfigError) without a circular import.
# Re-exported here unchanged so every ``journal_svc.PostingError`` caller
# and ``except journal_svc.PostingError`` handler keeps working.


# F-04: roles allowed to override a closed period-lock. Bookkeepers and
# viewers cannot bypass a period lock regardless of the reason they type;
# they must hand off to an accountant or admin. Owners outrank admins in
# the role hierarchy so they are included here too — excluding them would
# leave a tenant's primary owner unable to override their own lock.
_OVERRIDE_ROLES: frozenset[str] = frozenset({"admin", "accountant", "owner"})


# ---------------------------------------------------------------------- #
# Extended audit modes (Wave C, CHARTER §7.2 / §12.1)                    #
# ---------------------------------------------------------------------- #
# Three modes govern whether a POSTED (or REVERSED) entry may be edited:
#
#   immutable — posted entries can only be reversed, never edited. The
#       Community-only default (CHARTER §6.1) and the fail-safe fallback
#       used whenever entitlement or the stored value can't be trusted.
#   open      — posted entries are freely editable; every edit is logged
#       (caller's responsibility — see journal_entries.update()).
#   hybrid    — editable up to period-close, immutable after. Reuses the
#       F-04 period-lock mechanism (_check_period_lock) so a closed
#       period behaves identically for edits as it does for new posts,
#       including the admin/accountant/owner + reason override path.
#
# ``company.audit_mode`` (CompanyScoped) is the SINGLE source of truth —
# NOT the global, non-tenant-scoped ``Setting`` key of the same name this
# module used to read (orphaned: no route ever wrote it, so it sat
# permanently pinned at its 'immutable' default — see 0185's migration
# docstring for the full history). That Setting row is left in place
# (harmless, unread) rather than dropped, to avoid an unrelated data
# migration in this wave.
AUDIT_MODE_IMMUTABLE = "immutable"
AUDIT_MODE_OPEN = "open"
AUDIT_MODE_HYBRID = "hybrid"
VALID_AUDIT_MODES: frozenset[str] = frozenset(
    {AUDIT_MODE_IMMUTABLE, AUDIT_MODE_OPEN, AUDIT_MODE_HYBRID}
)


async def effective_audit_mode(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    extended_audit_modes_entitled: bool | None = None,
) -> str:
    """Resolve the audit mode that governs posted-entry edits for a company.

    Fails CLOSED to ``AUDIT_MODE_IMMUTABLE`` — the only mode Community is
    entitled to (CHARTER §6.1: "immutable ledger only. Not configurable")
    — in every one of these cases:

    * the company row can't be found (defensive; should not happen for a
      real ``entry.company_id``);
    * the stored ``audit_mode`` value isn't one of the three CHARTER
      strings (corrupt data, or a pre-0185 value that somehow survived
      the vocabulary migration);
    * the caller is not entitled to ``FLAG_EXTENDED_AUDIT_MODES`` at
      their effective edition — this is the belt-and-braces half of the
      gate: a below-tier install that already has a non-immutable value
      sitting in the column (e.g. from before this wave shipped, when
      the old validator accepted ``mutable``/``draft`` with no tier
      check at all) must NOT get open/hybrid behaviour just because the
      column says so.

    ``extended_audit_modes_entitled`` should be the caller's per-request
    resolution via ``features.feature_enabled_for_request`` — the same
    "resolve at the API layer, pass a bool down" pattern Wave B used for
    ``sae_relay_entitled`` (see ``services/customer_email.py``). Passing
    ``None`` (no request context — e.g. a CLI/system caller) falls back
    to the process-wide singleton edition via ``features.is_enabled``.
    """
    company = await session.get(Company, company_id)
    stored = company.audit_mode if company is not None else AUDIT_MODE_IMMUTABLE
    if stored not in VALID_AUDIT_MODES:
        return AUDIT_MODE_IMMUTABLE
    if stored == AUDIT_MODE_IMMUTABLE:
        return AUDIT_MODE_IMMUTABLE

    entitled = (
        extended_audit_modes_entitled
        if extended_audit_modes_entitled is not None
        else features_svc.is_enabled(features_svc.FLAG_EXTENDED_AUDIT_MODES)
    )
    return stored if entitled else AUDIT_MODE_IMMUTABLE


async def enforce_posted_edit_gate(
    session: AsyncSession,
    entry: JournalEntry,
    *,
    extended_audit_modes_entitled: bool | None = None,
    override_reason: str | None = None,
    actor_role: str | None = None,
) -> None:
    """Raise ``PostingError`` if editing ``entry`` isn't allowed right now.

    No-op for a DRAFT entry — every mode permits editing a draft freely;
    this only fires once an entry has left DRAFT (POSTED or REVERSED),
    matching the pre-existing ``entry.status != EntryStatus.DRAFT`` check
    this replaces.

    This is the SINGLE enforcement point for both callers: the legacy
    ``update_draft`` below, and ``services.journal_entries.update`` (the
    actually-reachable ``PATCH /api/v1/journal_entries/{id}`` path) —
    see that module for why both need to call it, not just one.
    """
    if entry.status == EntryStatus.DRAFT:
        return

    mode = await effective_audit_mode(
        session,
        entry.company_id,
        extended_audit_modes_entitled=extended_audit_modes_entitled,
    )
    if mode == AUDIT_MODE_IMMUTABLE:
        raise PostingError(
            "Cannot edit a posted entry in immutable mode — reverse instead"
        )
    if mode == AUDIT_MODE_HYBRID:
        # Editable only pre-period-close. Reuses the F-04 override path
        # (admin/accountant/owner + a real reason can still edit a closed
        # period) — see effective_audit_mode's docstring / module report
        # for why that's a deliberate, flagged design choice rather than
        # an unconditional "immutable after close".
        await _check_period_lock(
            session, entry.company_id, entry.entry_date, override_reason, actor_role
        )
    # AUDIT_MODE_OPEN: no further gate — editable unconditionally.


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
            "saebooks.api.v1.auth.resolve_tenant_id and thread it through."
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
    override_reason: str | None = None,
    actor_role: str | None = None,
    extended_audit_modes_entitled: bool | None = None,
) -> JournalEntry:
    """Update a journal entry (draft or, per its company's audit mode,
    posted/reversed). Despite the name, this is no longer draft-only —
    see ``enforce_posted_edit_gate`` for the per-company/per-mode gate
    that replaced the old blanket-``immutable`` check.

    NOTE: this function has zero live callers in the API/MCP surface as
    of Wave C — ``services.journal_entries.update`` is the actually
    reachable ``PATCH /api/v1/journal_entries/{id}`` implementation and
    has its own call to the same ``enforce_posted_edit_gate``. This
    function is kept working (and its Setting-read bug fixed, per the
    Wave C brief) because tests reference it directly, not because
    anything in production calls it today — flagged in the Wave C
    report.
    """
    entry = await get(session, entry_id, tenant_id=tenant_id)
    await enforce_posted_edit_gate(
        session,
        entry,
        extended_audit_modes_entitled=extended_audit_modes_entitled,
        override_reason=override_reason,
        actor_role=actor_role,
    )

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
                "fees and commissions earned by the agency should credit revenue accounts."
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
            "or 'PAYG-W withholding applied') to post this entry."
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
    """Snapshot a TaxTreatment onto every line via the company's
    per-jurisdiction tax engine.

    KMD-formula support Packet 3 (scope §3.4 point 1, the RC-FANOUT
    prerequisite): this used to hardcode ``get_engine("AU")`` for every
    post (the M0 docstring's "until then every post is AU"). It now
    resolves ``Company.jurisdiction`` for ``entry.company_id`` and
    dispatches through ``get_engine(...)`` — the registry/stub pattern
    already existed in ``tax_engine.__init__`` since M0, so this is
    "plumb the one caller", not "build a dispatcher". Every EXISTING
    company defaults to ``jurisdiction="AU"`` (``Company.jurisdiction``'s
    column default) and no production path sets it otherwise yet, so
    this resolves to the exact same ``AUTaxEngine`` for every AU post —
    byte-identical behaviour, just reached generically instead of
    hardcoded. NZ/UK remain unbuilt stubs (``NotImplementedError``,
    unchanged); EE is now a real engine (``tax_engine.ee.EETaxEngine``).

    Runs AFTER ``gst_svc.auto_post_gst_lines`` so the auto-added GST
    Collected/Paid line gets its own snapshot too (direction='none'
    because GST liability/asset accounts aren't in the input/output
    sets — they're plumbing, not BAS-reportable themselves).
    """
    company_jurisdiction = (
        await session.execute(
            select(Company.jurisdiction).where(Company.id == entry.company_id)
        )
    ).scalar_one_or_none() or "AU"
    engine = get_engine(company_jurisdiction)
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
                    TaxCode.id,
                    TaxCode.code,
                    TaxCode.rate,
                    TaxCode.reporting_type,
                    TaxCode.tax_family,
                ).where(TaxCode.id.in_(tc_ids))
            )
        ).all()
        if tc_ids
        else []
    )
    tc_meta = {row[0]: (row[1], row[2], row[3], row[4]) for row in tc_rows}

    # (line, treatments-list, tax_family) tuples to materialise as
    # component rows after the compute loop + a flush (so every line has
    # an id). Packet 3: a line may now yield MORE than one treatment
    # (EE reverse-charge fan-out) — was a single ``treatment`` before.
    _pending_components: list[tuple[JournalLine, list[Any], str | None]] = []
    # Critic-round-3 fix: a reversal entry is identified structurally
    # (``reversal_of_id`` is set on construction in ``reverse()``, before
    # this function ever runs on it) rather than inferred from
    # ``gst_amount is None`` — see the gate comment below for why the two
    # signals must be told apart now that a RC line can legitimately have
    # gst_amount=None on a FRESH post too.
    is_reversal_entry = entry.reversal_of_id is not None
    for ln in entry.lines:
        # debit + credit captures whichever side is non-zero (exactly
        # one is > 0 on a balanced line) — gives the line amount
        # without branching on direction.
        amount = (ln.debit or Decimal("0")) + (ln.credit or Decimal("0"))
        tc_code, tc_rate, tc_reporting, tc_family = (None, None, None, None)
        if ln.tax_code_id is not None and ln.tax_code_id in tc_meta:
            tc_code, tc_rate, tc_reporting, tc_family = tc_meta[ln.tax_code_id]
        ctx = PostingContext(
            company_id=entry.company_id,
            jurisdiction=company_jurisdiction,
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
        # Packet 3 — every engine implements compute_components (most
        # trivially as [self.compute(ctx)]); the dispatcher always calls
        # this, never .compute() directly, so a jurisdiction that needs
        # 2+ components on one line (EE's reverse-charge output+input
        # fan-out) needs no separate post-time code path. The FIRST
        # treatment is what gets snapshotted onto the JSONB column —
        # matches the pre-Packet-3 single-treatment shape exactly for
        # every engine that returns a length-1 list (AU, and EE outside
        # reverse-charge).
        treatments = engine.compute_components(ctx)
        ln.tax_treatment = treatments[0].to_jsonable()

        # M1.5 · T2 — also record each treatment as a normalised
        # tax-component row (1:many-ready) alongside the JSONB snapshot;
        # skip lines with no tax dimension to avoid zero-rows on every
        # plain GL line. Collected here and inserted by FK after a flush
        # (below) — async SQLAlchemy can't append to the unloaded
        # (lazy="raise") collection.
        #
        # Gate: emit component(s) when gst_amount is not None (unchanged
        # single-component behaviour from pre-Packet-3) OR — critic-
        # round-3 fix — when the engine returned a genuine reverse-charge
        # fan-out (len(treatments) > 1) on a NON-reversal entry.
        #
        # Why the OR is needed: a fresh EU-acquisition reverse-charge
        # line's natural shape has gst_amount=None — the foreign
        # supplier's invoice carries no VAT to copy in; self-assessment
        # is exactly what reverse charge means, and
        # EETaxEngine._compute_reverse_charge's ``_derive_tax`` already
        # falls back to ``base*rate/100`` for this case. The OLD gate
        # (gst_amount-only) silently dropped ALL components for that
        # line — the JSONB ``tax_treatment`` snapshot above still showed
        # the derived non-zero tax, but no JournalLineTaxComponent rows
        # were ever inserted, so KMD boxes 1_RC/5_RC (role-keyed,
        # component-only — no gst_amount fallback, unlike the account-
        # type-keyed boxes) silently read 0 for a posting that should
        # have moved them. Box 6 (account-type "purchase" bucket) still
        # picked up the base, producing a filable-but-wrong return (an
        # EU acquisition with no self-assessed VAT anywhere).
        #
        # Why NOT is_reversal_entry guards the OR: ``journal.reverse()``
        # posts reversal lines with tax_code_id copied but gst_amount=
        # None precisely so a plain line contributes NO GST (long-
        # standing behaviour the void-netting tests pin) — that part of
        # the gate is UNCHANGED. Without excluding reversals here, a
        # reversed RC line would ALSO satisfy ``len(treatments) > 1`` and
        # get its own components — but the role-keyed aggregation query
        # sums every matching component with no debit/credit netting, so
        # that would DOUBLE the RC contribution instead of zeroing it.
        # Reversing/voiding a posted RC line already relies on excluding
        # the entry from the aggregation window, not a symmetric negative
        # component — documented, accepted, unchanged (see
        # ``tax_engine.ee``'s module docstring, "Void/reversal caveat").
        if ln.tax_code_id is not None and not is_reversal_entry and (
            ln.gst_amount is not None or len(treatments) > 1
        ):
            _pending_components.append((ln, treatments, tc_family))

    # Finding 3 — a reversal entry MIRRORS the original entry's tax
    # components onto its own (debit/credit-swapped) lines, matched by
    # ``line_no`` (``reverse()`` copies each line's ``line_no`` verbatim).
    # The mirror carries the SAME positive base/tax as the original; the
    # aggregator (``tax_return_generator._aggregate_ledger_by_box``) signs
    # a reversal entry's contribution negative, so the original (+tax) and
    # its reversal (-tax) cancel. Both the REVERSED original and the
    # POSTED reversal are in REPORTABLE_STATUSES, so without this the base
    # cancelled (via its debit/credit sign) but the tax did not — the bug
    # findings 3/11 catch. We mirror rather than re-derive because the
    # reversal line carries ``gst_amount=None`` and the engine's
    # base*rate fallback would recompute the wrong figure; copying the
    # original component is exact. Supersedes the earlier "reversal emits
    # NO components" rule (which left tax un-netted).
    if is_reversal_entry and entry.reversal_of_id is not None:
        orig_rows = (
            await session.execute(
                select(JournalLine.line_no, JournalLineTaxComponent)
                .join(
                    JournalLineTaxComponent,
                    JournalLineTaxComponent.journal_line_id == JournalLine.id,
                )
                .where(JournalLine.entry_id == entry.reversal_of_id)
            )
        ).all()
        if orig_rows:
            by_line_no: dict[int, list[JournalLineTaxComponent]] = defaultdict(list)
            for line_no, comp in orig_rows:
                by_line_no[line_no].append(comp)
            await session.flush()
            for ln in entry.lines:
                for comp in by_line_no.get(ln.line_no, ()):
                    session.add(
                        JournalLineTaxComponent(
                            journal_line_id=ln.id,
                            company_id=entry.company_id,
                            tenant_id=entry.tenant_id,
                            tax_family=comp.tax_family,
                            component_role=comp.component_role,
                            ref_tax_code=comp.ref_tax_code,
                            rate_applied=comp.rate_applied,
                            base_amount=comp.base_amount,
                            tax_amount=comp.tax_amount,
                            direction=comp.direction,
                            sequence=comp.sequence,
                        )
                    )

    # Ensure every line has an id, then insert components directly by FK
    # (never touching the lazy="raise" JournalLine.tax_components collection).
    if _pending_components:
        await session.flush()
        for ln, treatments, tc_family in _pending_components:
            n = len(treatments)
            for seq, treatment in enumerate(treatments):
                session.add(
                    JournalLineTaxComponent(
                        journal_line_id=ln.id,
                        company_id=entry.company_id,
                        tenant_id=entry.tenant_id,
                        tax_family=tc_family or "vat_gst",
                        component_role=_component_role(seq, n, treatment),
                        ref_tax_code=treatment.code,
                        rate_applied=treatment.rate,
                        base_amount=treatment.base,
                        # Packet 3: per-COMPONENT tax, not the line's raw
                        # gst_amount — required for a reverse-charge line's
                        # two components to carry independently-correct
                        # amounts (a future partial-deduction input
                        # component would differ from its output sibling).
                        # For the single-component case (n == 1, every
                        # AU/EE non-reverse-charge line) this is IDENTICAL
                        # to ln.gst_amount by construction: both engines'
                        # compute() sets treatment.tax = ctx.gst_amount
                        # whenever gst_amount is not None (and this branch
                        # only runs when it is not None) — so the
                        # pre-Packet-3 "mirror ln.gst_amount, never the
                        # base*rate fallback" guarantee still holds for
                        # every existing single-component line.
                        tax_amount=treatment.tax,
                        direction=treatment.direction,
                        sequence=seq,
                    )
                )


def _component_role(seq: int, n: int, treatment: Any) -> str:
    """Map a (position, treatment) pair to a
    ``JournalLineTaxComponent.component_role`` string (Packet 3).

    The single-component case (``n == 1`` — every line today except an
    EE reverse-charge acquisition) is always ``"standard"``, matching
    pre-Packet-3 behaviour exactly. A multi-component line names each
    role from the treatment's own ``direction`` — ``"reverse_charge_output"``
    / ``"reverse_charge_input"`` are the two roles
    ``journal_line_tax_component.py``'s docstring already documents as
    the model's intended reverse-charge vocabulary; a hypothetical
    future stacked-tax engine (CGST/SGST-style) with a direction-less
    multi-component split falls back to a positional
    ``"component_<n>"`` label rather than guessing.
    """
    if n == 1:
        return "standard"
    if treatment.direction == "output":
        return "reverse_charge_output"
    if treatment.direction == "input":
        return "reverse_charge_input"
    return f"component_{seq}"


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

    # ATOMICITY FIX: do NOT commit the reversal here. The reversal post and
    # the original status flip must share ONE transaction, otherwise a guard
    # failure on the original UPDATE (e.g. origin=UNKNOWN legacy entries) leaves
    # the already-committed reversal as an orphan. Use post_in_txn (no commit)
    # and a single trailing commit so the whole reverse is all-or-nothing.
    await session.flush()

    # Auto-post the reversal WITHIN this txn — pass actor_role through so the
    # period-lock override gate fires consistently. Provenance:
    # origin=REVERSAL, source = the original journal entry being reversed.
    reversal = await post_in_txn(
        session,
        reversal.id,
        posted_by=posted_by,
        override_reason=override_reason,
        actor_role=actor_role,
        origin=JournalOrigin.REVERSAL,
        source_type="journal_entry",
        source_id=original.id,
    )

    # Mark original as reversed — same txn as the reversal post above, so a
    # guard rejection here rolls the reversal back too (no orphan).
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
        # JournalLine has no tenant_id column of its own (0055's "line/child
        # tables" carve-out), so audit_svc.snapshot_row's default
        # getattr(obj, "tenant_id", None) auto-detection can't see it here —
        # pass the parent entry's tenant_id explicitly so the audit_snapshots
        # row is tenant-stamped at capture time (Wave C / audit_snapshots
        # RLS remediation) instead of relying on the migration-time backfill.
        await audit_svc.snapshot_row(
            session, line,
            action="delete",
            performed_by=performed_by,
            reason=f"cascade-from journal_entry {entry.id}",
            tenant_id=entry.tenant_id,
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
