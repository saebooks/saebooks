"""Cashbook edition — auto-journal generator for sole-trader UX.

The cashbook UI sits on top of one service function:
``record_cashbook_entry``. Every cashbook entry compiles to a real
``JournalEntry`` with two lines (non-registered) or a ``JournalEntry``
with two lines + an auto-posted GST line (registered, via
``services.gst.auto_post_gst_lines`` at JE post time).

See ``docs/cashbook-edition-design.md`` for the full design and
``services/cashbook_categories.py`` for the picker taxonomy.

Public surface
--------------
- ``record_cashbook_entry`` — create + post a cashbook JE.
- ``CashbookError`` (with subclasses ``CashbookNotConfigured``,
  ``CashbookCurrencyError``, ``CashbookCategoryError``) — typed error
  hierarchy the API surfaces as 4xx with stable ``code`` values.

The service deliberately does NOT cover GET/list/summary endpoints —
those read straight from ``journal_entries`` filtered by
``attachments->'cashbook_meta' IS NOT NULL`` and live in their own
read-side service.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services import journal as journal_svc
from saebooks.services.cashbook_categories import (
    CashbookCategory,
    UnknownCashbookCategory,
    resolve_account_id_override,
    resolve_for_company,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CashbookError(Exception):
    """Base class for cashbook-service errors. Each subclass exposes a
    stable ``.code`` string the API echoes in error responses so
    clients can branch without parsing English."""

    code: str = "cashbook_error"


class CashbookNotConfigured(CashbookError):
    """Company is in cashbook mode but missing prerequisites
    (default bank account, non-AUD base currency, etc.)."""

    code = "cashbook_not_configured"


class CashbookCurrencyError(CashbookError):
    """Cashbook is AUD-only in v1; the company has a different
    ``base_currency``. Multi-currency cashbook is a 2027+ problem."""

    code = "cashbook_currency_unsupported"


class CashbookCategoryError(CashbookError):
    """Category code is unknown, hidden, or being used for the wrong
    direction (e.g. trying to log INC_SALES as expense)."""

    code = "cashbook_category_invalid"


class CashbookAccountResolutionError(CashbookError):
    """The category's ``default_account_code`` (or per-company
    override) does not resolve to an account in this company's chart
    of accounts. Caller should seed the chart of accounts."""

    code = "cashbook_account_unresolved"


class CashbookEntryNotFound(CashbookError):
    """The referenced cashbook entry does not exist for this company,
    or exists as a non-cashbook JE (no ``cashbook_meta`` stamp)."""

    code = "cashbook_entry_not_found"


class CashbookEntryNotEditable(CashbookError):
    """The referenced cashbook entry is not in a state that supports
    edit/void (e.g. already REVERSED, or DRAFT — cashbook entries
    are auto-posted so DRAFT shouldn't normally happen)."""

    code = "cashbook_entry_not_editable"


class CashbookSetupError(CashbookError):
    """Onboarding / mode-switch refused. Subclass-less by design; the
    message carries the why (e.g. "company has existing journal entries
    — switch to cashbook is not supported mid-life")."""

    code = "cashbook_setup_refused"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _ResolvedCategory:
    """The output of category resolution: the metadata + the actual
    ``Account`` row (and account_id) the line should reference."""

    category: CashbookCategory
    account_id: uuid.UUID


async def _resolve_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Company:
    """Load the company, with tenant guard. Raises ``CashbookError``
    if not found, not in cashbook mode, or misconfigured."""
    stmt = select(Company).where(
        Company.id == company_id,
        Company.tenant_id == tenant_id,
    )
    company = (await session.execute(stmt)).scalar_one_or_none()
    if company is None:
        raise CashbookError(f"Company {company_id} not found in tenant")
    if company.bookkeeping_mode != "cashbook":
        raise CashbookNotConfigured(
            f"Company {company_id} is not in cashbook mode "
            f"(bookkeeping_mode={company.bookkeeping_mode!r})"
        )
    if company.cashbook_default_bank_account_id is None:
        # Defensive: CHECK constraint should make this unreachable for
        # properly-onboarded companies. Surface a clean typed error
        # rather than letting a NULL FK explode downstream.
        raise CashbookNotConfigured(
            "Cashbook default bank account is not set for this company. "
            "Pick a bank account in Settings before recording entries."
        )
    if company.base_currency != "AUD":
        raise CashbookCurrencyError(
            f"Cashbook supports AUD only in v1 "
            f"(company base_currency={company.base_currency!r})"
        )
    return company


async def _resolve_category(
    session: AsyncSession,
    *,
    company: Company,
    category_code: str,
    direction: Literal["income", "expense"],
) -> _ResolvedCategory:
    """Look up the category, apply per-company overrides, and resolve
    the target account_id by chart-of-accounts code lookup (or
    explicit account-id override).

    The TX_TRANSFER category is rejected here — transfers go through a
    separate service surface because they need *two* bank accounts.
    """
    try:
        category = resolve_for_company(category_code, company.cashbook_categories)
    except UnknownCashbookCategory as e:
        raise CashbookCategoryError(str(e)) from e

    if category.direction == "transfer":
        raise CashbookCategoryError(
            f"Category {category_code} is a bank transfer, not an "
            "income/expense — use the transfer endpoint."
        )
    if category.direction != direction:
        raise CashbookCategoryError(
            f"Category {category_code} is "
            f"{category.direction!r}, but request is {direction!r}."
        )

    # Per-company account-id override wins; fall back to default code lookup.
    override_id = resolve_account_id_override(
        category_code, company.cashbook_categories
    )
    if override_id:
        stmt = select(Account.id).where(
            Account.id == uuid.UUID(override_id),
            Account.company_id == company.id,
        )
        account_id = (await session.execute(stmt)).scalar_one_or_none()
        if account_id is None:
            raise CashbookAccountResolutionError(
                f"Per-company account_id override for category "
                f"{category_code} ({override_id}) is not in this "
                "company's chart of accounts."
            )
        return _ResolvedCategory(category=category, account_id=account_id)

    if not category.default_account_code:
        # Should only happen for TX_TRANSFER, which we rejected above.
        raise CashbookCategoryError(
            f"Category {category_code} has no default account."
        )

    stmt = select(Account.id).where(
        Account.code == category.default_account_code,
        Account.company_id == company.id,
    )
    account_id = (await session.execute(stmt)).scalar_one_or_none()
    if account_id is None:
        raise CashbookAccountResolutionError(
            f"Category {category_code} maps to account code "
            f"{category.default_account_code!r} which is not in this "
            "company's chart of accounts. Seed the chart of accounts."
        )
    return _ResolvedCategory(category=category, account_id=account_id)


def _quantize_money(value: Decimal) -> Decimal:
    """Round to two decimal places, half-even (banker's rounding) —
    matches what Numeric(14,2) will store anyway, but normalising up
    front means the JE rows we create round-trip without surprises."""
    return value.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def record_cashbook_entry(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    entry_date: date,
    description: str | None,
    amount: Decimal,
    direction: Literal["income", "expense"],
    category_code: str,
    gst_amount: Decimal | None = None,
    idempotency_key: str,
    actor: str | None = None,
) -> JournalEntry:
    """Compile a cashbook entry into a posted ``JournalEntry``.

    Parameters
    ----------
    db:
        Open ``AsyncSession``. The function commits.
    tenant_id, company_id:
        Tenant-scoped lookup; ``company_id`` must belong to
        ``tenant_id`` and be in ``bookkeeping_mode='cashbook'``.
    entry_date:
        Effective date of the entry. Honours ``period_locks`` exactly
        as full-edition JE posting does (rejected with the existing
        period-lock error).
    description:
        Free-text. Surfaces on the cashbook list and on the JE.
    amount:
        Gross, always positive. For income, money received; for
        expense, money out. Enforced > 0.
    direction:
        ``income`` or ``expense``. ``transfer`` is a separate service
        surface — see ``record_cashbook_transfer`` (not yet built).
    category_code:
        One of the codes in
        ``services.cashbook_categories.DEFAULT_CATEGORIES``, after
        per-company overrides resolve.
    gst_amount:
        Override the category default GST. ``None`` means "use the
        category default × company gst_registered". Pass
        ``Decimal("0")`` to suppress GST on a one-off (e.g. a
        GST-registered trader booking a GST-free expense). Always the
        GST portion of the gross ``amount`` (i.e. for $110 inclusive
        GST, pass 10 or None).
    idempotency_key:
        Required. The same ``(company_id, idempotency_key)`` returns
        the same ``JournalEntry`` regardless of how many times the
        client retries. Race-safe via the
        ``uq_je_cashbook_idempotency`` partial unique index.
    actor:
        Audit-log actor identifier. Threaded through to ``posted_by``.

    Returns
    -------
    The posted ``JournalEntry`` (status=POSTED, with auto-posted GST
    lines attached if applicable).

    Raises
    ------
    CashbookError subclasses (see module docstring) for misconfigured
    company, unknown category, unresolved account, multi-currency
    request, or non-positive amount. Period-lock failures raise
    ``saebooks.services.journal.PostingError`` from the post step.
    """
    if amount is None or amount <= Decimal("0"):
        raise CashbookError(
            f"Cashbook amount must be positive (got {amount!r})"
        )
    amount = _quantize_money(Decimal(amount))

    company = await _resolve_company(db, company_id, tenant_id)

    # Idempotency replay shortcut. Cheap query; if a JE already exists
    # for this (company, key) pair we return it. The unique index
    # below is the source of truth — this lookup avoids a guaranteed
    # IntegrityError on the happy retry path.
    existing = await _find_by_idempotency(
        db, company_id=company.id, idempotency_key=idempotency_key
    )
    if existing is not None:
        return existing

    resolved = await _resolve_category(
        db,
        company=company,
        category_code=category_code,
        direction=direction,
    )

    # GST split. Non-registered traders never get a GST line; the JE
    # is a clean two-line entry. For registered traders, the GST
    # amount lives on the category line as ``gst_amount`` so
    # ``gst.auto_post_gst_lines`` will add the matching DR/CR GST
    # account line at post time.
    if not company.gst_registered:
        line_gst: Decimal | None = None
        net_amount = amount
    else:
        if gst_amount is None:
            # Default rate × gross / (1+rate) for inclusive amounts.
            rate = resolved.category.gst_default
            line_gst = (
                _quantize_money(amount * rate / (Decimal("1") + rate))
                if rate > Decimal("0")
                else None
            )
        else:
            line_gst = (
                _quantize_money(Decimal(gst_amount))
                if gst_amount and gst_amount > Decimal("0")
                else None
            )
        net_amount = amount - (line_gst or Decimal("0"))

    bank_account_id = company.cashbook_default_bank_account_id

    # Build the two-line JE. For income: DR Bank, CR Category.
    # For expense: DR Category, CR Bank. ``amount`` is gross; the
    # category line carries the net + gst metadata.
    if direction == "income":
        lines: list[dict[str, object]] = [
            {
                "account_id": bank_account_id,
                "debit": amount,
                "credit": Decimal("0"),
                "description": description,
            },
            {
                "account_id": resolved.account_id,
                "debit": Decimal("0"),
                "credit": net_amount,
                "description": description,
                "gst_amount": line_gst,
            },
        ]
    else:  # expense
        lines = [
            {
                "account_id": resolved.account_id,
                "debit": net_amount,
                "credit": Decimal("0"),
                "description": description,
                "gst_amount": line_gst,
            },
            {
                "account_id": bank_account_id,
                "debit": Decimal("0"),
                "credit": amount,
                "description": description,
            },
        ]

    # Create the draft JE through the existing service path so the
    # ref sequence, line validation and tenant scoping are uniform.
    draft = await journal_svc.create_draft(
        db,
        company_id=company.id,
        entry_date=entry_date,
        description=description,
        lines=lines,
        tenant_id=tenant_id,
    )

    # Stamp the cashbook metadata onto attachments BEFORE posting so
    # the unique index catches a concurrent duplicate at flush time
    # (and before GST lines are auto-posted, in case auto-posting
    # fails for an unrelated reason — we want the idempotency stamp
    # tied to the JE's existence, not its successful post).
    draft.attachments = {
        **(draft.attachments or {}),
        "cashbook_meta": {
            "idempotency_key": idempotency_key,
            "category_code": resolved.category.code,
            "direction": direction,
            "gross_amount": str(amount),
            "gst_amount": str(line_gst) if line_gst is not None else None,
            "actor": actor,
        },
    }
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent writer beat us to the unique index. Roll back our
        # draft and return the winner. This races on the network but
        # not on the database — the index guarantees exactly one JE
        # per (company, key).
        await db.rollback()
        existing = await _find_by_idempotency(
            db, company_id=company.id, idempotency_key=idempotency_key
        )
        if existing is None:
            # Should not happen — the IntegrityError implies a row
            # exists. Re-raise as a typed error rather than something
            # opaque.
            raise CashbookError(
                "Idempotency conflict but no existing entry found — "
                "DB state inconsistent."
            )
        return existing

    # Post — runs GST auto-post + balance check + period-lock check
    # against the existing ``services.journal`` invariants.
    posted = await journal_svc.post(
        db,
        draft.id,
        posted_by=actor,
        tenant_id=tenant_id,
    )

    # Re-load with lines so callers see the full posted shape (incl.
    # any auto-posted GST line).
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.id == posted.id)
    )
    return (await db.execute(stmt)).scalar_one()


async def void_cashbook_entry(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    entry_id: uuid.UUID,
    reason: str | None = None,
    actor: str | None = None,
) -> JournalEntry:
    """Void a posted cashbook entry by posting a reversing JE.

    Idempotent: re-voiding an already-REVERSED entry returns the
    existing reversal JE rather than failing. This makes
    ``replace_cashbook_entry`` safe to retry after a partial failure
    (void succeeded, record_cashbook_entry crashed, user retries).

    The original entry's ``status`` flips to ``REVERSED``. The
    reversal JE itself does NOT carry ``cashbook_meta``, so the
    cashbook list/summary endpoints filter it out automatically.

    Raises
    ------
    CashbookEntryNotFound:
        The entry does not exist for this company, or exists as a
        non-cashbook JE.
    CashbookEntryNotEditable:
        Status is ``DRAFT`` (shouldn't happen — cashbook always
        auto-posts) or anything other than POSTED / REVERSED.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(
            JournalEntry.id == entry_id,
            JournalEntry.company_id == company_id,
        )
    )
    je = (await db.execute(stmt)).scalar_one_or_none()
    if je is None:
        raise CashbookEntryNotFound(
            f"Cashbook entry {entry_id} not found in this company"
        )
    if not (je.attachments or {}).get("cashbook_meta"):
        # Hide non-cashbook JEs from this surface — same 404 contract
        # as the GET-by-id route.
        raise CashbookEntryNotFound(
            f"Entry {entry_id} is not a cashbook entry"
        )

    # Idempotent path: already reversed, return the existing reversal.
    if je.status == EntryStatus.REVERSED:
        rev_stmt = (
            select(JournalEntry)
            .options(selectinload(JournalEntry.lines))
            .where(JournalEntry.reversal_of_id == je.id)
        )
        existing_rev = (await db.execute(rev_stmt)).scalar_one_or_none()
        if existing_rev is not None:
            return existing_rev
        # Reversal JE missing for a REVERSED original — corrupt state.
        # Fall through to re-create the reversal so the audit trail
        # gets repaired.

    if je.status not in (EntryStatus.POSTED, EntryStatus.REVERSED):
        raise CashbookEntryNotEditable(
            f"Cashbook entry {entry_id} cannot be voided: "
            f"status={je.status!r}"
        )

    return await journal_svc.reverse(
        db,
        entry_id,
        posted_by=actor,
        override_reason=reason or "cashbook void",
        tenant_id=tenant_id,
    )


async def replace_cashbook_entry(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    entry_id: uuid.UUID,
    entry_date: date,
    description: str | None,
    amount: Decimal,
    direction: Literal["income", "expense"],
    category_code: str,
    gst_amount: Decimal | None = None,
    idempotency_key: str,
    actor: str | None = None,
) -> JournalEntry:
    """Void original + create a replacement cashbook entry.

    Cashbook PATCH semantics: the design says "never edit in place,
    always void & re-create" so the audit trail stays intact. The
    original gets a reversal JE; a new JE is created with the new
    payload. The new JE's ``cashbook_meta`` is stamped with
    ``replaces_id`` pointing at the original; the original is left
    pointing at its reversal via ``reversal_of_id`` (set by
    ``journal.reverse``).

    Idempotency: ``idempotency_key`` belongs to the *new* entry. The
    same key on retry returns the same replacement JE — and the void
    step is itself idempotent (already-REVERSED is a no-op), so the
    retry case after a mid-flow failure recovers cleanly.

    Returns the new (replacement) ``JournalEntry``.
    """
    # Idempotency replay shortcut. If a JE already exists for this
    # key, return it — the original is presumed already voided.
    existing = await _find_by_idempotency(
        db, company_id=company_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        return existing

    # Void the original (idempotent: returns existing reversal if
    # already REVERSED, raises NotEditable otherwise).
    await void_cashbook_entry(
        db=db,
        tenant_id=tenant_id,
        company_id=company_id,
        entry_id=entry_id,
        reason=f"Replaced by new cashbook entry (key={idempotency_key})",
        actor=actor,
    )

    # Create the replacement via the normal path.
    new_je = await record_cashbook_entry(
        db=db,
        tenant_id=tenant_id,
        company_id=company_id,
        entry_date=entry_date,
        description=description,
        amount=amount,
        direction=direction,
        category_code=category_code,
        gst_amount=gst_amount,
        idempotency_key=idempotency_key,
        actor=actor,
    )

    # Stamp the chain link on the new entry so admin / audit tools can
    # walk back to the originating row.
    meta = dict((new_je.attachments or {}).get("cashbook_meta") or {})
    meta["replaces_id"] = str(entry_id)
    new_je.attachments = {
        **(new_je.attachments or {}),
        "cashbook_meta": meta,
    }
    await db.commit()

    # Reload with lines for caller.
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.id == new_je.id)
    )
    return (await db.execute(stmt)).scalar_one()


async def _find_by_idempotency(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    idempotency_key: str,
) -> JournalEntry | None:
    """Return the JE previously stamped with this key, or None.

    Reads ``attachments->'cashbook_meta'->>'idempotency_key'``. Cheap
    because the unique partial index covers exactly this access path.
    """
    stmt = (
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.attachments["cashbook_meta"]["idempotency_key"].astext
            == idempotency_key,
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def setup_cashbook_mode(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    bank_account_id: uuid.UUID,
    actor: str | None = None,
) -> Company:
    """Flip a company into cashbook mode and pin its default bank account.

    Idempotent for already-cashbook companies (just updates the bank
    account pointer). Refuses to flip a 'full' company that already has
    journal entries — design says full→cashbook is not supported, so a
    company carrying ledger history can never be downgraded by accident.

    Parameters
    ----------
    db, tenant_id, company_id:
        Standard scoping. ``company_id`` must belong to ``tenant_id``.
    bank_account_id:
        Must be an existing ``Account`` on this company. The CHECK
        constraint at the DB layer (``ck_cashbook_requires_bank``)
        enforces non-NULL when the mode is cashbook; we additionally
        verify the account belongs to this company before assignment.
    actor:
        Audit-log actor identifier (currently unused by this helper —
        accepted for symmetry with other cashbook service entry points).

    Returns
    -------
    The refreshed ``Company`` row, with ``bookkeeping_mode='cashbook'``
    and ``cashbook_default_bank_account_id`` set.

    Raises
    ------
    CashbookError:
        Company not found / wrong tenant.
    CashbookSetupError:
        Bank account does not belong to this company; or company is
        currently in 'full' mode and already has journal entries.
    """
    company_stmt = select(Company).where(
        Company.id == company_id,
        Company.tenant_id == tenant_id,
    )
    company = (await db.execute(company_stmt)).scalar_one_or_none()
    if company is None:
        raise CashbookError(f"Company {company_id} not found in tenant")

    # Bank account must belong to this company.
    bank_stmt = select(Account.id).where(
        Account.id == bank_account_id,
        Account.company_id == company_id,
    )
    bank_id = (await db.execute(bank_stmt)).scalar_one_or_none()
    if bank_id is None:
        raise CashbookSetupError(
            f"Bank account {bank_account_id} is not in this company's "
            "chart of accounts."
        )

    # full → cashbook flip: Round-2 audit fix #10 makes this a
    # first-class downgrade per ``[[cashbook-upgrade-downgrade-policy]]``.
    # Delegate to downgrade_full_to_cashbook for the AR-balance check
    # so the policy is enforced consistently regardless of entry point.
    # (Previously this raised "switching mid-life is not supported".)
    if company.bookkeeping_mode != "cashbook":
        # downgrade does its own commit + flips the mode; pass through
        # the resolved bank_id so the caller-supplied account is
        # respected even when an old cashbook_default_bank_account_id
        # exists from a prior phase.
        return await downgrade_full_to_cashbook(
            db=db,
            tenant_id=tenant_id,
            company_id=company_id,
            bank_account_id=bank_id,
            actor=actor,
        )

    company.bookkeeping_mode = "cashbook"
    company.cashbook_default_bank_account_id = bank_id
    company.version = (company.version or 1) + 1
    await db.commit()
    await db.refresh(company)
    return company


async def _ar_outstanding_invoice_ids(
    db: AsyncSession, company_id: uuid.UUID
) -> list[tuple[uuid.UUID, str | None, Decimal]]:
    """Return [(invoice_id, number, balance)] for invoices with
    non-zero AR balance on this company.

    Used by ``downgrade_full_to_cashbook`` to refuse the mode flip
    when AR > 0 — the schema invariant (``[[cashbook-upgrade-downgrade-policy]]``)
    is that no AR balances exist in cashbook mode.
    """
    from saebooks.models.invoice import Invoice, InvoiceStatus
    stmt = (
        select(Invoice.id, Invoice.number, Invoice.total, Invoice.amount_paid)
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.archived_at.is_(None),
        )
    )
    rows = (await db.execute(stmt)).all()
    out: list[tuple[uuid.UUID, str | None, Decimal]] = []
    for inv_id, number, total, paid in rows:
        balance = Decimal(str(total)) - Decimal(str(paid or "0"))
        if balance > Decimal("0"):
            out.append((inv_id, number, balance))
    return out


async def downgrade_full_to_cashbook(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    bank_account_id: uuid.UUID | None = None,
    actor: str | None = None,
) -> Company:
    """Flip ``bookkeeping_mode='full'`` → ``'cashbook'`` (Round-2 audit
    fix #10 — bidirectional bookkeeping_mode).

    Schema invariant (per ``[[cashbook-upgrade-downgrade-policy]]``):
    no AR balances + cashbook mode. So this function refuses the flip
    when any POSTED invoice still has an unpaid balance. The error
    body lists the offending invoice IDs + numbers so the user can
    chase them down (or write them off via a credit note + AR clear).

    The journal entries themselves are NOT collapsed. Issue-time
    Dr AR / Cr Income / Cr GST JEs stay in the ledger, alongside the
    receipt-time Dr Bank / Cr AR JEs — their NET effect is the same
    as a cashbook-mode Dr Bank / Cr Income / Cr GST, and the ledger
    round-trips cleanly per the memory rule "the ledger must round-trip
    cleanly — if downgrade would drop a journal entry, that's a bug
    in the strict-subset invariant".

    ``bank_account_id``:
        Required if the company doesn't already have
        ``cashbook_default_bank_account_id`` set (i.e. was never in
        cashbook mode before). If the company has an existing pointer
        from a prior cashbook phase, that's reused unless this arg
        overrides it.

    Raises
    ------
    CashbookError:
        Company not found / wrong tenant.
    CashbookSetupError:
        AR > 0 (with invoice list in the message);
        company already in cashbook mode (use ``setup_cashbook_mode``
        for the idempotent bank-account update);
        bank_account_id required but not provided.
    """
    company_stmt = select(Company).where(
        Company.id == company_id,
        Company.tenant_id == tenant_id,
    )
    company = (await db.execute(company_stmt)).scalar_one_or_none()
    if company is None:
        raise CashbookError(f"Company {company_id} not found in tenant")

    if company.bookkeeping_mode == "cashbook":
        raise CashbookSetupError(
            "Company is already in cashbook mode — no downgrade needed."
        )

    # Schema invariant: no open AR in cashbook mode.
    outstanding = await _ar_outstanding_invoice_ids(db, company.id)
    if outstanding:
        sample = ", ".join(
            f"{num or 'unnumbered'}={bal}"
            for _id, num, bal in outstanding[:10]
        )
        more = (
            f" (and {len(outstanding) - 10} more)"
            if len(outstanding) > 10
            else ""
        )
        raise CashbookSetupError(
            "Cannot downgrade to cashbook with open AR balances. "
            f"{len(outstanding)} invoice(s) have an outstanding balance: "
            f"{sample}{more}. Receive or write off each one (via credit "
            "note + allocation) before downgrading."
        )

    # Resolve bank account: caller override → existing pointer → error.
    target_bank_id = bank_account_id or company.cashbook_default_bank_account_id
    if target_bank_id is None:
        raise CashbookSetupError(
            "Cashbook mode requires a default bank account. Pass "
            "bank_account_id or call setup_cashbook_mode first."
        )

    # Verify the bank account belongs to this company.
    bank_stmt = select(Account.id).where(
        Account.id == target_bank_id,
        Account.company_id == company_id,
    )
    bank_id = (await db.execute(bank_stmt)).scalar_one_or_none()
    if bank_id is None:
        raise CashbookSetupError(
            f"Bank account {target_bank_id} is not in this company's "
            "chart of accounts."
        )

    company.bookkeeping_mode = "cashbook"
    company.cashbook_default_bank_account_id = bank_id
    company.version = (company.version or 1) + 1
    await db.commit()
    await db.refresh(company)
    return company


async def upgrade_cashbook_to_full(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    actor: str | None = None,
) -> Company:
    """One-way flip: ``bookkeeping_mode='cashbook'`` → ``'full'``.

    Refuses if the company is already in 'full' mode (409 from the API
    layer) — there is no further state change to perform, and the
    caller likely has a stale view.

    Cashbook entries already posted are real journal entries, so the
    upgrade is purely a UX flag flip — no data migration runs.

    Returns the refreshed company. Raises ``CashbookSetupError`` if
    the company is not currently in cashbook mode (so the caller can
    surface a precise 409).

    ``cashbook_default_bank_account_id`` is cleared atomically with the
    mode flip to satisfy the CHECK constraint added in migration 0126
    (``ck_cashbook_default_bank_requires_cashbook_mode``).  The user can
    re-attach a preferred bank account through the full-mode settings once
    the upgrade is complete.
    """
    company_stmt = select(Company).where(
        Company.id == company_id,
        Company.tenant_id == tenant_id,
    )
    company = (await db.execute(company_stmt)).scalar_one_or_none()
    if company is None:
        raise CashbookError(f"Company {company_id} not found in tenant")

    if company.bookkeeping_mode != "cashbook":
        raise CashbookSetupError(
            f"Company is not in cashbook mode "
            f"(bookkeeping_mode={company.bookkeeping_mode!r}); "
            "nothing to upgrade."
        )

    # Backfill A/R-on-issue JEs for any invoices that were issued in
    # cashbook mode and are still OPEN (amount_paid < total). Paid
    # invoices need no backfill: the cashbook payment posted Dr Bank /
    # Cr Income / Cr GST, which is net-equivalent to the full-mode pair.
    from saebooks.services import edition as edition_svc
    await edition_svc.backfill_invoice_journals(
        db, company.id, actor=actor
    )

    company.bookkeeping_mode = "full"
    # Clear the cashbook default bank atomically with the mode flip so the
    # CHECK constraint ck_cashbook_default_bank_requires_cashbook_mode is
    # satisfied in a single UPDATE.  The user can re-attach a bank account
    # via full-mode settings after the upgrade.
    company.cashbook_default_bank_account_id = None
    company.version = (company.version or 1) + 1
    await db.commit()
    await db.refresh(company)
    return company


__all__ = [
    "CashbookError",
    "CashbookNotConfigured",
    "CashbookCurrencyError",
    "CashbookCategoryError",
    "CashbookAccountResolutionError",
    "CashbookEntryNotFound",
    "CashbookEntryNotEditable",
    "CashbookSetupError",
    "record_cashbook_entry",
    "void_cashbook_entry",
    "replace_cashbook_entry",
    "setup_cashbook_mode",
    "downgrade_full_to_cashbook",
    "upgrade_cashbook_to_full",
]
