"""AP bill service — create, update, post, void, archive.

Mirror of ``services/invoices.py``. All GL-impacting operations go
through ``services/journal.py`` — bills never touch
``journal_entries`` directly. GST auto-posting is wired up in
``gst.py``: a line with ``tax_code_id`` + ``gst_amount`` on an EXPENSE
account gets a matching DR GST Paid appended during post.

Posting journal shape (ex-GST line treatment):

    Dr Expense (per line) ......... line_subtotal
    Dr GST Paid ................... line_tax (auto-posted by gst.py)
    Cr Trade Creditors (AP) ....... total
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bill import Bill, BillLine, BillStatus
from saebooks.models.contact import Contact
from saebooks.models.item import Item
from saebooks.models.journal import JournalOrigin
from saebooks.models.tax_code import TaxCode
from saebooks.money import decimal_places_for, round_money
from saebooks.services import control_accounts as control_accounts_svc
from saebooks.services import items as items_svc
from saebooks.services import journal as journal_svc
from saebooks.services import numbering
from saebooks.services import settings as settings_svc
from saebooks.services import terms as terms_svc
from saebooks.services.tax_engine.ee import RC_DUAL_REPORTING_TYPES

# Settings key naming the "VAT self-assessed (reverse charge) payable"
# liability account a reverse-charge EU-acquisition bill credits for its
# self-assessed output VAT (see _post_rc_gl / post_bill). Distinct from
# the input-side GST Paid account (which auto_post_gst_lines books off
# the expense line's gst_amount) — an RC posting books BOTH: an
# output-role liability here and an input-role receivable via GST Paid.
RC_PAYABLE_ACCOUNT_SETTING_KEY = "gst_reverse_charge_payable_account_code"

_FOURPLACES = Decimal("0.0001")


class BillError(ValueError):
    """Raised on bill validation or state-transition failure."""


# ---------------------------------------------------------------------- #
# Math                                                                    #
# ---------------------------------------------------------------------- #


def _q2(value: Decimal, places: int = 2) -> Decimal:
    """ROUND_HALF_UP to a currency's minor unit (default AUD/base — 2)."""
    return round_money(value, places)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_FOURPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _LineInput:
    description: str
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    project_id: uuid.UUID | None
    item_id: uuid.UUID | None
    retention_pct: Decimal = Decimal("0")
    tracking_vehicle_id: str | None = None


def _compute_line_totals(
    line: _LineInput, tax_rate: Decimal, places: int = 2
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (subtotal, tax, total) — add-on (ex-GST) tax treatment.

    Amounts round to ``places`` — the document currency's minor unit.
    """
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor, places)
    tax = _q2(subtotal * tax_rate / Decimal("100"), places)
    total = subtotal + tax
    return subtotal, tax, total


async def _resolve_tax_rate(
    session: AsyncSession,
    tax_code_id: uuid.UUID | None,
    company_id: uuid.UUID | None = None,
) -> Decimal:
    if tax_code_id is None:
        return Decimal("0")
    if company_id is not None:
        result = await session.execute(
            select(TaxCode).where(
                TaxCode.id == tax_code_id, TaxCode.company_id == company_id
            )
        )
        tc = result.scalars().first()
    else:
        tc = await session.get(TaxCode, tax_code_id)
    if tc is None:
        raise BillError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _rc_tax_code_ids(
    session: AsyncSession, lines: list[BillLine]
) -> set[uuid.UUID]:
    """The subset of a bill's ``tax_code_id``s whose company-side
    ``TaxCode.reporting_type`` is an EU-acquisition reverse-charge tag
    (``RC_DUAL_REPORTING_TYPES`` — ``rc_eu_acq_goods`` /
    ``rc_eu_acq_services``). Empty for an ordinary (domestic) bill, so
    the RC branch in ``post_bill`` is a no-op and the posting is
    byte-identical to the pre-RC path.

    Finding 2/8 fix (was ``_reject_unsupported_reverse_charge``, a
    critic-round-4 HARD BLOCK): ``post_bill`` now posts the economically
    correct reverse-charge GL instead of rejecting the bill — Accounts
    Payable is credited for the NET invoice only (the foreign supplier
    charges no VAT), the self-assessed OUTPUT VAT is booked as a
    liability on the ``RC_PAYABLE_ACCOUNT_SETTING_KEY`` account, and the
    deductible INPUT VAT is booked as a receivable by the existing
    ``auto_post_gst_lines`` off the expense line's ``gst_amount`` (the
    two normally net to zero cash effect). See ``post_bill``.
    """
    tc_ids = {line.tax_code_id for line in lines if line.tax_code_id is not None}
    if not tc_ids:
        return set()
    result = await session.execute(
        select(TaxCode.id, TaxCode.reporting_type).where(TaxCode.id.in_(tc_ids))
    )
    return {tid for (tid, rt) in result.all() if rt in RC_DUAL_REPORTING_TYPES}


async def _get_rc_payable_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    """Resolve the reverse-charge self-assessed OUTPUT-VAT payable
    liability account for an RC bill, via the
    ``RC_PAYABLE_ACCOUNT_SETTING_KEY`` setting. Raises ``BillError`` (a
    config error, loud — never a silently-unbalanced posting) when an RC
    bill is posted but the account is unset or does not resolve, mirroring
    ``auto_post_gst_lines``'s treatment of a missing GST account code."""
    code = await settings_svc.get(session, RC_PAYABLE_ACCOUNT_SETTING_KEY, "")
    code = (str(code) if code is not None else "").strip()
    acct = None
    if code:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == code,
                )
            )
        ).scalar_one_or_none()
    if acct is None:
        raise BillError(
            "Cannot post a reverse-charge EU-acquisition bill: the "
            f"self-assessed VAT payable account setting "
            f"{RC_PAYABLE_ACCOUNT_SETTING_KEY!r} ({code!r}) does not "
            "resolve to an account in this company's chart. Set it to a "
            "real liability account so the output-side self-assessed VAT "
            "can be booked (AP is credited for the net invoice only)."
        )
    return acct


# ---------------------------------------------------------------------- #
# CRUD                                                                    #
# ---------------------------------------------------------------------- #


async def create_draft(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    issue_date: date,
    due_date: date,
    supplier_reference: str | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
) -> Bill:
    bill = Bill(
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        supplier_reference=supplier_reference,
        notes=notes,
        status=BillStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
    )
    session.add(bill)
    await session.flush()

    if lines:
        await _replace_lines(session, bill, lines, company_id=company_id)

    await _recalc(session, bill)
    await session.commit()
    return await get(session, bill.id)


async def _replace_lines(
    session: AsyncSession,
    bill: Bill,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    # Hard-delete existing lines via SQL so the identity map doesn't
    # carry stale rows — same pattern as invoices.
    await session.execute(
        sa_delete(BillLine).where(BillLine.bill_id == bill.id)
    )
    await session.flush()
    session.expire(bill, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        project_id = raw.get("project_id")
        if isinstance(project_id, str) and project_id:
            project_id = uuid.UUID(project_id)
        elif not project_id:
            project_id = None

        item_id = raw.get("item_id")
        if isinstance(item_id, str) and item_id:
            item_id = uuid.UUID(item_id)
        elif not item_id:
            item_id = None

        account_id = _as_uuid(raw["account_id"])
        # If this line is an item receipt, the GL account MUST be the
        # item's inventory_account_id — otherwise the stock movement
        # and the journal fall out of sync. Force-override silently so
        # a user who picks the "wrong" account on the form still gets
        # a consistent post.
        if isinstance(item_id, uuid.UUID):
            item = await session.get(Item, item_id)
            if item is None:
                raise BillError(f"Unknown item {item_id}")
            account_id = item.inventory_account_id
        elif company_id is not None:
            acct_chk = await session.execute(
                select(Account.id).where(
                    Account.id == account_id, Account.company_id == company_id
                )
            )
            if acct_chk.scalar_one_or_none() is None:
                raise BillError(f"account {account_id} not found")

        raw_ret = raw.get("retention_pct")
        retention_pct = (
            Decimal(str(raw_ret)) if raw_ret not in (None, "", "0", 0)
            else Decimal("0")
        )
        if not (Decimal("0") <= retention_pct <= Decimal("100")):
            raise BillError(
                f"retention_pct must be between 0 and 100 (got {retention_pct})"
            )

        raw_vid = raw.get("tracking_vehicle_id")
        tracking_vehicle_id = str(raw_vid).strip() if raw_vid else None

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=account_id,
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
            project_id=project_id if isinstance(project_id, uuid.UUID) else None,
            item_id=item_id if isinstance(item_id, uuid.UUID) else None,
            retention_pct=retention_pct,
            tracking_vehicle_id=tracking_vehicle_id or None,
        )
        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id, company_id)
        subtotal, tax, total = _compute_line_totals(
            line_input, tax_rate, decimal_places_for(bill.currency)
        )
        session.add(
            BillLine(
                bill_id=bill.id,
                line_no=i,
                description=line_input.description,
                account_id=line_input.account_id,
                tax_code_id=line_input.tax_code_id,
                quantity=line_input.quantity,
                unit_price=line_input.unit_price,
                discount_pct=line_input.discount_pct,
                line_subtotal=subtotal,
                line_tax=tax,
                line_total=total,
                project_id=line_input.project_id,
                item_id=line_input.item_id,
                retention_pct=line_input.retention_pct,
                tracking_vehicle_id=line_input.tracking_vehicle_id,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, bill: Bill) -> None:
    lines = (
        await session.execute(
            select(BillLine).where(BillLine.bill_id == bill.id)
        )
    ).scalars().all()
    doc_places = decimal_places_for(bill.currency)
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    bill.subtotal = _q2(Decimal(subtotal), doc_places)
    bill.tax_total = _q2(Decimal(tax), doc_places)
    bill.total = bill.subtotal + bill.tax_total

    # Foreign-currency shadow totals. Same pattern as invoices — sum
    # per-line base contributions so header base_total matches the sum
    # of per-line journal lines that post_bill will emit.
    rate = Decimal(str(bill.fx_rate or Decimal("1")))
    base_subtotal = sum(
        (_q2(ln.line_subtotal * rate) for ln in lines), Decimal("0")
    )
    base_tax = sum((_q2(ln.line_tax * rate) for ln in lines), Decimal("0"))
    bill.base_subtotal = _q2(Decimal(base_subtotal))
    bill.base_tax_total = _q2(Decimal(base_tax))
    bill.base_total = bill.base_subtotal + bill.base_tax_total
    bill.base_amount_paid = _q2(Decimal(bill.amount_paid) * rate)


async def get(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Bill:
    """Fetch a bill by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id raises ``BillError`` (treated as not found),
    so cross-tenant probes 404 even if the underlying row exists.
    Belt-and-braces complement to FORCE RLS at the DB layer.
    """
    stmt = (
        select(Bill)
        .options(selectinload(Bill.lines), selectinload(Bill.one_off_vendor))
        .where(Bill.id == bill_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(Bill.tenant_id == tenant_id)
    result = await session.execute(stmt)
    bill = result.scalar_one_or_none()
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    return bill


async def list_bills(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: BillStatus | None = None,
    contact_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Bill]:
    stmt = (
        select(Bill)
        .options(selectinload(Bill.lines), selectinload(Bill.one_off_vendor))
        .where(Bill.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Bill.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Bill.status == status)
    if contact_id is not None:
        stmt = stmt.where(Bill.contact_id == contact_id)
    stmt = stmt.order_by(Bill.issue_date.desc(), Bill.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    supplier_reference: str | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Bill:
    bill = await get(session, bill_id, tenant_id=tenant_id)
    if bill.status != BillStatus.DRAFT:
        raise BillError(
            f"Cannot edit bill {bill.id} in state {bill.status.value}; "
            "void the existing bill and raise a new one instead."
        )
    if contact_id is not None:
        bill.contact_id = contact_id
    if issue_date is not None:
        bill.issue_date = issue_date
    if due_date is not None:
        bill.due_date = due_date
    if supplier_reference is not None:
        bill.supplier_reference = supplier_reference
    if notes is not None:
        bill.notes = notes
    if currency is not None:
        bill.currency = currency.upper()
    if fx_rate is not None:
        bill.fx_rate = fx_rate
    if lines is not None:
        await _replace_lines(session, bill, lines)
    await _recalc(session, bill)
    await session.commit()
    return await get(session, bill.id)


# ---------------------------------------------------------------------- #
# Post / void                                                             #
# ---------------------------------------------------------------------- #


async def _get_ap_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    # Packet 4b: resolves companies.ap_control_account_code, falling
    # back to the AU convention "2-1200" — see services/control_accounts.py.
    return await control_accounts_svc.get_ap_account(
        session, company_id, error_cls=BillError
    )


async def _get_retentions_payable_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "2-1850",
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise BillError(
            "Retentions Payable account 2-1850 is missing — "
            "re-run the CoA seed or add account 2-1850 manually."
        )
    return acct


async def post_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Bill:
    bill = await get(session, bill_id)
    if bill.status == BillStatus.POSTED:
        raise BillError(f"Bill {bill.id} is already posted")
    if bill.status == BillStatus.VOIDED:
        raise BillError(f"Bill {bill.id} is voided; raise a new one")
    if not bill.lines:
        raise BillError("Cannot post a bill with no lines")
    if bill.total <= Decimal("0"):
        raise BillError(f"Cannot post bill with non-positive total {bill.total}")
    rc_tc_ids = await _rc_tax_code_ids(session, bill.lines)

    # Mint the internal bill number now (DRAFT never burns a number).
    if not bill.number:
        bill.number = await numbering.next_number(
            session, bill.company_id, "bill"
        )

    ap_account = await _get_ap_account(session, bill.company_id)
    ref = bill.supplier_reference or bill.number

    # Post the journal in base currency. AUD-only: rate=1, base_*=
    # unscaled, behaviour unchanged. Foreign-currency: per-line
    # Dr + GST are translated at the bill's rate.
    rate = Decimal(str(bill.fx_rate or Decimal("1")))

    # Calculate total retention amount across all lines (in base currency).
    # Retention is withheld from the ex-GST portion only — GST input tax
    # credit is claimed on the full invoice value per ATO requirements.
    total_retention_base = sum(
        _q2(_q2(line.line_subtotal * Decimal(str(line.retention_pct))) / Decimal("100") * rate)
        for line in bill.lines
    )

    journal_lines: list[dict[str, object]] = []
    # Self-assessed OUTPUT VAT on reverse-charge EU-acquisition lines
    # (finding 2/8). This is the VAT the foreign supplier did NOT charge:
    # it must NOT inflate Accounts Payable (nothing extra is owed to the
    # supplier), and it must be booked as an output-side liability. We
    # strip it out of the AP credit below and post it to the RC-payable
    # account instead; the matching deductible INPUT VAT is booked by
    # auto_post_gst_lines off the expense line's gst_amount (the two
    # normally net to a zero cash effect — scope §6's "output == input").
    rc_vat_base_total = Decimal("0")
    # One Dr line per expense/asset account per bill line; GST
    # auto-poster appends the matching Dr GST Paid. project_id rides
    # through so P&L-by-project can pick up cost-side postings.
    for line in bill.lines:
        line_base_subtotal = _q2(line.line_subtotal * rate)
        line_base_tax = (
            _q2(line.line_tax * rate) if line.line_tax > 0 else None
        )
        if line.tax_code_id in rc_tc_ids and line_base_tax:
            rc_vat_base_total += line_base_tax
        journal_lines.append(
            {
                "account_id": line.account_id,
                "description": f"{bill.number}: {line.description}",
                "debit": line_base_subtotal,
                "credit": Decimal("0"),
                "tax_code_id": line.tax_code_id,
                "gst_amount": line_base_tax,
                "project_id": line.project_id,
            }
        )

    # AP is credited for the invoice value LESS the self-assessed RC VAT
    # (which the supplier never charged). For an ordinary bill
    # rc_vat_base_total is 0, so ap_credit == bill.base_total exactly —
    # the pre-RC posting is byte-identical.
    ap_credit_full = _q2(bill.base_total - rc_vat_base_total)
    if total_retention_base > Decimal("0"):
        # Split Cr AP: Trade Creditors receives only the net-payable
        # portion; Retentions Payable receives the withheld amount.
        # Expense and GST are recognised in full (Dr side unchanged).
        retention_acct = await _get_retentions_payable_account(session, bill.company_id)
        net_ap = _q2(ap_credit_full - total_retention_base)
        journal_lines.append(
            {
                "account_id": ap_account.id,
                "description": f"Bill {bill.number} ({ref}) — net payable",
                "debit": Decimal("0"),
                "credit": net_ap,
            }
        )
        journal_lines.append(
            {
                "account_id": retention_acct.id,
                "description": f"Bill {bill.number}: retention held",
                "debit": Decimal("0"),
                "credit": total_retention_base,
            }
        )
    else:
        # Standard path — no retention, single Cr Trade Creditors line.
        journal_lines.append(
            {
                "account_id": ap_account.id,
                "description": f"Bill {bill.number} ({ref})",
                "debit": Decimal("0"),
                "credit": ap_credit_full,
            }
        )

    # Output-side self-assessed VAT liability for reverse-charge lines.
    if rc_vat_base_total > Decimal("0"):
        rc_payable_acct = await _get_rc_payable_account(session, bill.company_id)
        journal_lines.append(
            {
                "account_id": rc_payable_acct.id,
                "description": f"Bill {bill.number}: reverse-charge VAT self-assessed",
                "debit": Decimal("0"),
                "credit": rc_vat_base_total,
            }
        )

    entry = await journal_svc.create_draft(
        session,
        company_id=bill.company_id,
        tenant_id=bill.tenant_id,
        entry_date=bill.issue_date,
        description=f"Bill {bill.number} ({ref})",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        override_reason=override_reason,
        origin=JournalOrigin.BILL,
        source_type="bill",
        source_id=bill.id,
    )

    # Inventory stock movement: for every line with item_id, bump
    # on_hand_qty per the company's costing method. Unit cost is the
    # base-currency line subtotal divided by quantity — GST is excluded
    # (stays on the Dr GST Paid asset line), FX is already applied at
    # _recalc. Runs AFTER the journal posts so a failed journal doesn't
    # mutate stock. WAC blends the average; FIFO creates a cost layer;
    # quantity_only just tracks the on-hand count. Resolve the method
    # once per bill.
    costing_method = await items_svc.get_company_costing_method(
        session, bill.company_id
    )
    for line in bill.lines:
        if line.item_id is None:
            continue
        if line.quantity <= Decimal("0"):
            continue
        line_base_subtotal = _q2(line.line_subtotal * rate)
        unit_cost = _q4(line_base_subtotal / line.quantity)
        await items_svc.receive_stock(
            session,
            line.item_id,
            qty=line.quantity,
            unit_cost=unit_cost,
            method=costing_method,
            received_date=bill.issue_date,
        )

    bill.status = BillStatus.POSTED
    bill.journal_entry_id = posted.id
    bill.posted_at = datetime.now(UTC)
    bill.posted_by = posted_by
    await session.commit()
    return await get(session, bill.id)


async def void_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Bill:
    bill = await get(session, bill_id)
    if bill.status == BillStatus.VOIDED:
        return bill
    if bill.status == BillStatus.DRAFT:
        bill.status = BillStatus.VOIDED
        await session.commit()
        return bill
    if bill.amount_paid > Decimal("0"):
        raise BillError(
            f"Bill {bill.number} has payments or credit notes allocated — "
            "unallocate/void them before voiding."
        )
    if bill.journal_entry_id is None:
        raise BillError(f"Posted bill {bill.id} has no journal entry id")

    reversal = await journal_svc.reverse(
        session,
        bill.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void bill {bill.number}",
        tenant_id=bill.tenant_id,
    )
    bill.status = BillStatus.VOIDED
    bill.void_journal_entry_id = reversal.id
    await session.commit()
    return bill


async def archive(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Bill:
    bill = await get(session, bill_id, tenant_id=tenant_id)
    bill.archived_at = datetime.now(UTC)
    await session.commit()
    return bill


# ==========================================================================
# API-oriented service (cycle 8) — optimistic locking + change_log
#
# These functions are the API surface for /api/v1/bills.  They are
# intentionally separate from the legacy posting pipeline above so the
# two surfaces can evolve independently.
# ==========================================================================

from sqlalchemy import func  # noqa: E402

from saebooks.services import audit_log as audit_log_svc  # noqa: E402
from saebooks.services import change_log as change_log_svc  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Bill) -> None:
        super().__init__(
            f"Bill {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Columns serialised into change_log.payload
# ---------------------------------------------------------------------------

_BILL_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "number",
    "supplier_reference",
    "issue_date",
    "due_date",
    "status",
    "subtotal",
    "tax_total",
    "total",
    "currency",
    "notes",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise_bill(bill: Bill) -> dict:
    from decimal import Decimal as _D

    data: dict = {}
    for key in _BILL_COLUMNS:
        val = getattr(bill, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, _D):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _get_with_lines(
    session: AsyncSession, bill_id: uuid.UUID
) -> Bill | None:
    result = await session.execute(
        select(Bill)
        .options(selectinload(Bill.lines), selectinload(Bill.one_off_vendor))
        .where(Bill.id == bill_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    status: BillStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Bill], int]:
    """Return (bills, total_count) — excludes archived bills."""
    base_where = [
        Bill.company_id == company_id,
        Bill.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(Bill.contact_id == contact_id)
    if status is not None:
        base_where.append(Bill.status == status)
    if date_from is not None:
        base_where.append(Bill.issue_date >= date_from)
    if date_to is not None:
        base_where.append(Bill.issue_date <= date_to)

    count_stmt = select(func.count()).select_from(Bill).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Bill)
        .options(selectinload(Bill.lines), selectinload(Bill.one_off_vendor))
        .where(*base_where)
        .order_by(Bill.issue_date.desc(), Bill.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    bills = list((await session.execute(stmt)).scalars().unique().all())
    return bills, total


async def api_get(
    session: AsyncSession,
    bill_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Bill | None:
    """Fetch a single bill with its lines. Returns None if not found.

    P0 cross-tenant leak fix: when ``tenant_id`` is supplied, the
    lookup is filtered by tenant — a foreign-tenant id returns
    ``None`` even if the row exists. The parameter is keyword-only
    and optional so existing callers (the legacy posting pipeline)
    keep working unchanged; the API layer always supplies it.
    """
    if tenant_id is None and company_id is None:
        return await _get_with_lines(session, bill_id)
    clauses = [Bill.id == bill_id]
    if tenant_id is not None:
        clauses.append(Bill.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Bill.company_id == company_id)
    result = await session.execute(
        select(Bill)
        .options(selectinload(Bill.lines), selectinload(Bill.one_off_vendor))
        .where(*clauses)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Cross-tenant FK validation (CIVL-1 P0 fix)
# ---------------------------------------------------------------------------
#
# RLS at the DB layer (FORCE ROW LEVEL SECURITY + tenant_isolation policy
# from migration 0055) catches cross-tenant FK injection only when the API
# connects through a NOBYPASSRLS role. In dev / older deployments the API
# may still run as the schema owner, where RLS is silently a no-op. The
# helpers below add a belt-and-braces tenant scope check at the service
# layer so the bills endpoint cannot accept a foreign-tenant contact_id,
# account_id, or tax_code_id even if RLS is unenforced.
#
# Behaviour: a foreign-tenant or unknown id raises ``BillError`` with the
# message ``"<entity> not found in current tenant"``. The router maps
# ``BillError`` to HTTP 422, matching the contract the medium-civil-
# contractor critic expected.


async def _validate_contact_company_and_tenant(
    session: AsyncSession,
    contact_id: uuid.UUID,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``BillError`` if ``contact_id`` does not belong to ``tenant_id``
    or to ``company_id`` (Lane 1/2 P0-3 -- cross-company FK on invoice/bill create).
    """
    result = await session.execute(
        select(Contact.id).where(
            Contact.id == contact_id,
            Contact.company_id == company_id,
            Contact.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise BillError(
            "contact_company_mismatch: contact does not belong to this company"
        )


async def _validate_account_tenant(
    session: AsyncSession,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``BillError`` if ``account_id`` does not belong to ``tenant_id``."""
    result = await session.execute(
        select(Account.id).where(
            Account.id == account_id,
            Account.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise BillError("account not found in current tenant")


async def _validate_tax_code_tenant(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``BillError`` if ``tax_code_id`` does not belong to ``tenant_id``."""
    result = await session.execute(
        select(TaxCode.id).where(
            TaxCode.id == tax_code_id,
            TaxCode.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise BillError("tax_code not found in current tenant")


async def _validate_line_fks(
    session: AsyncSession,
    lines: list[dict],
    tenant_id: uuid.UUID,
) -> None:
    """Validate every line's ``account_id`` + optional ``tax_code_id``.

    Each id must belong to ``tenant_id``; otherwise ``BillError`` is
    raised with the same message contract as the helpers above.
    """
    for raw in lines:
        account_raw = raw.get("account_id")
        if account_raw is not None:
            account_id = (
                account_raw
                if isinstance(account_raw, uuid.UUID)
                else uuid.UUID(str(account_raw))
            )
            await _validate_account_tenant(session, account_id, tenant_id)

        tax_code_raw = raw.get("tax_code_id")
        if tax_code_raw:
            tax_code_id = (
                tax_code_raw
                if isinstance(tax_code_raw, uuid.UUID)
                else uuid.UUID(str(tax_code_raw))
            )
            await _validate_tax_code_tenant(session, tax_code_id, tenant_id)


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    contact_id: uuid.UUID,
    issue_date: date,
    due_date: date | None = None,
    lines: list[dict] | None = None,
    reference: str | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    commit: bool = True,
) -> Bill:
    """Create a bill draft with version=1 and a change_log row.

    ``due_date`` may be omitted: when ``None`` it is derived from the supplier
    contact's default payment terms (``payment_terms_basis``/``_days``) via
    ``services/terms.compute_due_date`` — e.g. "30-day EOM". If the contact has
    no terms, it falls back to ``issue_date``.

    CIVL-1 P0 fix: ``contact_id`` and every line's ``account_id`` /
    ``tax_code_id`` are validated against ``tenant_id`` before any
    INSERT. Cross-tenant FK injection raises ``BillError`` (HTTP 422
    via the router).

    ``commit`` (default ``True``) preserves the historic self-committing
    behaviour. Pass ``commit=False`` to leave the row flushed-but-uncommitted
    so a caller (e.g. the PO→bill conversion) can commit the bill fact
    atomically with its own idempotency bookkeeping.
    """
    await _validate_contact_company_and_tenant(session, contact_id, company_id, tenant_id)
    if lines:
        await _validate_line_fks(session, lines, tenant_id)

    locked_through = await journal_svc.get_locked_through(session, company_id)
    if locked_through is not None and issue_date <= locked_through:
        raise BillError(
            f"Bill date {issue_date} falls inside locked period "
            f"(ends {locked_through}); contact your controller to adjust period lock"
        )

    # Derive due_date from the supplier's default payment terms when not given
    # (e.g. "30-day EOM"); fall back to issue_date if the contact has no terms.
    if due_date is None:
        contact = await session.get(Contact, contact_id)
        derived = terms_svc.compute_due_date(
            issue_date,
            contact.payment_terms_basis if contact else None,
            contact.payment_terms_days if contact else None,
        )
        due_date = derived if derived is not None else issue_date

    bill = Bill(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        supplier_reference=reference,
        notes=notes,
        status=BillStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
        version=1,
    )
    session.add(bill)
    await session.flush()
    await session.refresh(bill)

    if lines:
        await _replace_lines(session, bill, lines)
        await _recalc(session, bill)

    await session.flush()

    bill_loaded = await _get_with_lines(session, bill.id)
    assert bill_loaded is not None

    await change_log_svc.append(
        session,
        entity="bill",
        entity_id=bill_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise_bill(bill_loaded),
        version=bill_loaded.version,
    )
    if commit:
        await session.commit()
    return await _get_with_lines(session, bill_loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    bill_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    notes: str | None = None,
    reference: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
    lines: list[dict] | None = None,
) -> Bill:
    """Update a bill draft with optimistic locking + change_log.

    CIVL-1 P0 fix: when ``contact_id`` or ``lines`` are supplied, every
    referenced contact / account / tax_code is validated against the
    bill's owning ``tenant_id``. Cross-tenant FK injection raises
    ``BillError`` (HTTP 422 via the router).
    """
    bill = await _get_with_lines(session, bill_id)
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    if bill.version != expected_version:
        raise VersionConflict(bill)
    if bill.status != BillStatus.DRAFT:
        # Non-financial metadata (notes, supplier reference, due_date) may be
        # corrected on POSTED / VOIDED bills — mirrors the invoice allowlist;
        # none of it feeds totals, GST or the posted journal entry (due_date
        # only drives aging buckets and display). Anything financial stays
        # DRAFT-only: void the bill and raise a new one. issue_date stays
        # locked — it anchors the posted JE / GST period. The change is still
        # version-bumped and change_log'd like every other edit.
        financial_change = (
            contact_id is not None
            or issue_date is not None
            or currency is not None
            or fx_rate is not None
            or lines is not None
        )
        if financial_change:
            raise BillError(
                f"bill_not_draft: cannot edit bill {bill.id} in state "
                f"{bill.status.value}; void the existing bill and raise a new one instead."
            )

    if contact_id is not None:
        await _validate_contact_company_and_tenant(session, contact_id, bill.company_id, bill.tenant_id)
        bill.contact_id = contact_id
    if lines is not None:
        await _validate_line_fks(session, lines, bill.tenant_id)
    if issue_date is not None:
        bill.issue_date = issue_date
    if due_date is not None:
        bill.due_date = due_date
    if notes is not None:
        bill.notes = notes
    if reference is not None:
        bill.supplier_reference = reference
    if currency is not None:
        bill.currency = currency.upper()
    if fx_rate is not None:
        bill.fx_rate = fx_rate
    if lines is not None:
        await _replace_lines(session, bill, lines)
        await _recalc(session, bill)
    elif fx_rate is not None:
        await _recalc(session, bill)

    bill.version = bill.version + 1
    await session.flush()
    await session.refresh(bill)

    bill_loaded = await _get_with_lines(session, bill_id)
    assert bill_loaded is not None

    await change_log_svc.append(
        session,
        entity="bill",
        entity_id=bill_loaded.id,
        op="update",
        actor=actor,
        payload=_serialise_bill(bill_loaded),
        version=bill_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, bill_id)  # type: ignore[return-value]


async def api_void(
    session: AsyncSession,
    bill_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Bill:
    """Soft-delete (archive/void) a bill with optimistic locking + change_log."""
    bill = await _get_with_lines(session, bill_id)
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    if bill.version != expected_version:
        raise VersionConflict(bill)

    bill.archived_at = datetime.now(UTC)
    bill.status = BillStatus.VOIDED
    bill.version = bill.version + 1
    await session.flush()
    await session.refresh(bill)

    bill_loaded = await _get_with_lines(session, bill_id)
    assert bill_loaded is not None

    await change_log_svc.append(
        session,
        entity="bill",
        entity_id=bill_loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise_bill(bill_loaded),
        version=bill_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, bill_id)  # type: ignore[return-value]


async def api_post_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Bill:
    """Transition DRAFT → POSTED with JE generation, optimistic locking + change_log.

    Wraps the legacy ``post_bill()`` pipeline which mints the bill
    number, builds journal lines (Dr Expense / Dr GST Paid / Cr AP), calls
    ``journal_svc.post()``, and stamps ``journal_entry_id`` + ``posted_at``.

    When ``tenant_id`` is supplied the bill must belong to that tenant;
    a mismatch raises ``BillError("not found")`` so callers see a 404.
    """
    bill = await _get_with_lines(session, bill_id)
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    if tenant_id is not None and bill.tenant_id != tenant_id:
        raise BillError(f"Bill {bill_id} not found")
    if bill.version != expected_version:
        raise VersionConflict(bill)
    if bill.status == BillStatus.VOIDED:
        raise BillError(
            f"Bill {bill.id} is VOIDED and cannot be posted"
        )
    if bill.status == BillStatus.POSTED:
        raise BillError(f"Bill {bill.id} is already POSTED")
    if not bill.lines:
        raise BillError("Cannot post a bill with no lines")

    # Delegate to the legacy pipeline (mints number, builds JE, posts it,
    # commits internally). After this call the session is in a fresh state.
    # PostingError (period lock, trust commingling, balance) is a legacy
    # exception type; translate it to BillError so the router returns 422.
    try:
        bill = await post_bill(
            session,
            bill_id,
            posted_by=actor,
        )
    except journal_svc.PostingError as exc:
        raise BillError(str(exc)) from exc

    # Bump version + append change_log in the same transaction.
    bill.version = bill.version + 1
    await session.flush()
    await session.refresh(bill)

    bill_loaded = await _get_with_lines(session, bill_id)
    assert bill_loaded is not None

    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=bill_loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.BILL_POST,
            table_name="bills",
            row_id=str(bill_loaded.id),
            row_snapshot=_serialise_bill(bill_loaded),
        )
    await change_log_svc.append(
        session,
        entity="bill",
        entity_id=bill_loaded.id,
        op="post",
        actor=actor,
        payload=_serialise_bill(bill_loaded),
        version=bill_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, bill_id)  # type: ignore[return-value]


async def api_void_bill(
    session: AsyncSession,
    bill_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Bill:
    """Transition any non-VOIDED → VOIDED with JE reversal (if POSTED),
    optimistic locking + change_log.

    Wraps the legacy ``void_bill()`` pipeline which handles both the
    DRAFT case (no JE) and the POSTED case (reversal JE via
    ``journal_svc.reverse()``).

    When ``tenant_id`` is supplied the bill must belong to that tenant;
    a mismatch raises ``BillError("not found")`` so callers see a 404.
    """
    bill = await _get_with_lines(session, bill_id)
    if bill is None:
        raise BillError(f"Bill {bill_id} not found")
    if tenant_id is not None and bill.tenant_id != tenant_id:
        raise BillError(f"Bill {bill_id} not found")
    if bill.version != expected_version:
        raise VersionConflict(bill)
    if bill.status == BillStatus.VOIDED:
        raise BillError(f"Bill {bill.id} is already VOIDED")

    # Delegate to legacy pipeline (handles JE reversal where needed, commits).
    bill = await void_bill(
        session,
        bill_id,
        posted_by=actor,
        override_reason=f"API void by {actor}",
    )

    # Bump version + append change_log.
    bill.version = bill.version + 1
    await session.flush()
    await session.refresh(bill)

    bill_loaded = await _get_with_lines(session, bill_id)
    assert bill_loaded is not None

    if actor_user_id is not None:
        await audit_log_svc.append(
            session,
            tenant_id=bill_loaded.tenant_id,
            actor_user_id=actor_user_id,
            action=audit_log_svc.AuditAction.BILL_VOID,
            table_name="bills",
            row_id=str(bill_loaded.id),
            row_snapshot=_serialise_bill(bill_loaded),
            reason=f"API void by {actor}",
        )
    await change_log_svc.append(
        session,
        entity="bill",
        entity_id=bill_loaded.id,
        op="void",
        actor=actor,
        payload=_serialise_bill(bill_loaded),
        version=bill_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, bill_id)  # type: ignore[return-value]
