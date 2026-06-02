"""AR invoice service — create, update, post, void, mark-sent.

All GL-impacting operations go through ``services/journal.py`` —
invoices never touch ``journal_entries`` directly. GST auto-posting
is already wired up in ``gst.py``: a line with ``tax_code_id`` +
``gst_amount`` on an INCOME account gets a matching CR GST Collected
appended during post.

Numbers come from ``services/numbering.py`` at post time, not
create time — that way DRAFTs don't burn numbers. ATO requires gap-
free tax-invoice numbering; the counter + row lock in numbering.py
guarantees that.

Posting journal shape (ex-GST line treatment):

    Dr Trade Debtors (AR control) ..... line_total
    Cr Income ......................... line_subtotal (per line)
    Cr GST Collected .................. line_tax (auto-posted by gst.py)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import func

from saebooks.models.account import Account
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus
from saebooks.models.item import Item
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bills_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import items as items_svc
from saebooks.services import journal as journal_svc
from saebooks.services import numbering
from saebooks.services import settings as settings_svc

_TWOPLACES = Decimal("0.01")


class InvoiceError(ValueError):
    """Raised on invoice validation or state-transition failure."""


# ---------------------------------------------------------------------- #
# Math                                                                    #
# ---------------------------------------------------------------------- #


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


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
    service_start_date: date | None = None
    service_end_date: date | None = None
    margin_acq_cost: Decimal | None = None
    retention_pct: Decimal = Decimal("0")
    is_trade_in: bool = False


def _compute_line_totals(
    line: _LineInput, tax_code: TaxCode | None
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (subtotal, tax, total) — add-on (ex-GST) tax treatment.

    For margin_scheme codes (Div 75 s66-50), GST = 1/11 × max(0, subtotal − acq_cost)
    rather than rate % of subtotal.
    """
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor)
    if tax_code is not None and tax_code.reporting_type == "margin_scheme":
        acq_cost = line.margin_acq_cost or Decimal("0")
        margin = max(Decimal("0"), subtotal - acq_cost)
        tax = _q2(margin / Decimal("11"))
    elif tax_code is not None:
        rate = Decimal(str(tax_code.rate or 0))
        tax = _q2(subtotal * rate / Decimal("100"))
    else:
        tax = Decimal("0")
    total = subtotal + tax
    return subtotal, tax, total


async def _resolve_tax_code(
    session: AsyncSession,
    tax_code_id: uuid.UUID | None,
    company_id: uuid.UUID | None = None,
) -> TaxCode | None:
    if tax_code_id is None:
        return None
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
        raise InvoiceError(f"tax_code {tax_code_id} not found")
    return tc


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
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    settlement_date: date | None = None,
) -> Invoice:
    ct_chk = await session.execute(
        select(Contact.id).where(
            Contact.id == contact_id, Contact.company_id == company_id
        )
    )
    if ct_chk.scalar_one_or_none() is None:
        raise InvoiceError(f"contact {contact_id} not found")
    inv = Invoice(
        company_id=company_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        notes=notes,
        payment_terms=payment_terms,
        status=InvoiceStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
        settlement_date=settlement_date,
    )
    session.add(inv)
    await session.flush()

    if lines:
        await _replace_lines(session, inv, lines, company_id=company_id)

    await _recalc(session, inv)
    await session.commit()
    return await get(session, inv.id)


async def _replace_lines(
    session: AsyncSession,
    inv: Invoice,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    # Hard-delete all existing lines via SQL so the back-populated
    # collection doesn't hold stale rows in the identity map.
    from sqlalchemy import delete as sa_delete
    await session.execute(
        sa_delete(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    )
    await session.flush()
    # Expire the relationship so the next access re-queries.
    session.expire(inv, ["lines"])

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
        # If this line is an item sale, force account_id to the item's
        # income_account_id — otherwise an operator could pick an
        # unrelated account on the form and the GL would not match
        # the inventory ledger. Silently corrective.
        if isinstance(item_id, uuid.UUID):
            item = await session.get(Item, item_id)
            if item is None:
                raise InvoiceError(f"Unknown item {item_id}")
            account_id = item.income_account_id
        elif company_id is not None:
            acct_chk = await session.execute(
                select(Account.id).where(
                    Account.id == account_id, Account.company_id == company_id
                )
            )
            if acct_chk.scalar_one_or_none() is None:
                raise InvoiceError(f"account {account_id} not found")

        ssd = raw.get("service_start_date")
        sed = raw.get("service_end_date")
        service_start = (
            date.fromisoformat(str(ssd)) if isinstance(ssd, str) and ssd
            else ssd if isinstance(ssd, date) else None
        )
        service_end = (
            date.fromisoformat(str(sed)) if isinstance(sed, str) and sed
            else sed if isinstance(sed, date) else None
        )

        raw_acq = raw.get("margin_acq_cost")
        margin_acq_cost = (
            Decimal(str(raw_acq)) if raw_acq not in (None, "", "0", 0) else None
        )

        raw_ret = raw.get("retention_pct")
        retention_pct = (
            Decimal(str(raw_ret)) if raw_ret not in (None, "", "0", 0)
            else Decimal("0")
        )
        if not (Decimal("0") <= retention_pct <= Decimal("100")):
            raise InvoiceError(
                f"retention_pct must be between 0 and 100 (got {retention_pct})"
            )

        is_trade_in = bool(raw.get("is_trade_in", False))
        raw_up = raw.get("unit_price", 0)
        unit_price = Decimal(str(raw_up))
        if is_trade_in and unit_price < Decimal("0"):
            raise InvoiceError(
                "Trade-in unit_price must be a positive value representing the "
                "trade-in vehicle's acquisition cost (not a negative discount)"
            )

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=account_id,
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=unit_price,
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
            project_id=project_id if isinstance(project_id, uuid.UUID) else None,
            item_id=item_id if isinstance(item_id, uuid.UUID) else None,
            service_start_date=service_start,
            service_end_date=service_end,
            margin_acq_cost=margin_acq_cost,
            retention_pct=retention_pct,
            is_trade_in=is_trade_in,
        )
        tax_code = await _resolve_tax_code(session, line_input.tax_code_id, company_id)
        subtotal, tax, total = _compute_line_totals(line_input, tax_code)
        session.add(
            InvoiceLine(
                invoice_id=inv.id,
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
                service_start_date=line_input.service_start_date,
                service_end_date=line_input.service_end_date,
                margin_acq_cost=line_input.margin_acq_cost,
                retention_pct=line_input.retention_pct,
                is_trade_in=line_input.is_trade_in,
            )
        )
    await session.flush()


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _recalc(session: AsyncSession, inv: Invoice) -> None:
    lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
        )
    ).scalars().all()
    # Trade-in lines are excluded from the invoice header totals. They do
    # not contribute to G1 (they are purchases, not sales) and will be
    # posted as a separate AP bill at post_invoice time.
    sale_lines = [ln for ln in lines if not ln.is_trade_in]
    subtotal = sum((ln.line_subtotal for ln in sale_lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in sale_lines), Decimal("0"))
    inv.subtotal = _q2(Decimal(subtotal))
    inv.tax_total = _q2(Decimal(tax))
    inv.total = inv.subtotal + inv.tax_total

    # Foreign-currency shadow totals. Computed from per-line base
    # contributions so header totals equal the sum of per-line values
    # that post_invoice will push into the journal (avoids a 1-cent
    # drift between Dr AR base_total and Cr income per line).
    rate = Decimal(str(inv.fx_rate or Decimal("1")))
    base_subtotal = sum(
        (_q2(ln.line_subtotal * rate) for ln in sale_lines), Decimal("0")
    )
    base_tax = sum((_q2(ln.line_tax * rate) for ln in sale_lines), Decimal("0"))
    inv.base_subtotal = _q2(Decimal(base_subtotal))
    inv.base_tax_total = _q2(Decimal(base_tax))
    inv.base_total = inv.base_subtotal + inv.base_tax_total
    # Preserve amount_paid → base_amount_paid at the invoice rate. Actual
    # realised-FX gain/loss happens in services/payments.py at post time
    # using the payment's rate vs the invoice's rate.
    inv.base_amount_paid = _q2(Decimal(inv.amount_paid) * rate)


async def get(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Invoice:
    """Fetch an invoice by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id raises ``InvoiceError`` (treated as not found),
    so cross-tenant probes 404 even if the underlying row exists.
    Belt-and-braces complement to FORCE RLS at the DB layer.
    """
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.one_off_customer))
        .where(Invoice.id == invoice_id)
    )
    if tenant_id is not None:
        stmt = stmt.where(Invoice.tenant_id == tenant_id)
    result = await session.execute(stmt)
    inv = result.scalar_one_or_none()
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    return inv


async def list_invoices(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: InvoiceStatus | None = None,
    contact_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Invoice]:
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.one_off_customer))
        .where(Invoice.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Invoice.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Invoice.status == status)
    if contact_id is not None:
        stmt = stmt.where(Invoice.contact_id == contact_id)
    stmt = stmt.order_by(Invoice.issue_date.desc(), Invoice.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def update_draft(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    lines: list[dict[str, object]] | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
    tenant_id: uuid.UUID | None = None,
    settlement_date: date | None = None,
) -> Invoice:
    inv = await get(session, invoice_id, tenant_id=tenant_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError(
            f"Cannot edit invoice {inv.id} in state {inv.status.value}; "
            "void the existing invoice and raise a new one instead."
        )
    if contact_id is not None:
        inv.contact_id = contact_id
    if issue_date is not None:
        inv.issue_date = issue_date
    if due_date is not None:
        inv.due_date = due_date
    if notes is not None:
        inv.notes = notes
    if payment_terms is not None:
        inv.payment_terms = payment_terms
    if currency is not None:
        inv.currency = currency.upper()
    if fx_rate is not None:
        inv.fx_rate = fx_rate
    if settlement_date is not None:
        inv.settlement_date = settlement_date
    if lines is not None:
        await _replace_lines(session, inv, lines)
    await _recalc(session, inv)
    await session.commit()
    return await get(session, inv.id)


# ---------------------------------------------------------------------- #
# Post / void                                                             #
# ---------------------------------------------------------------------- #


async def _get_ar_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "1-1200",
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise InvoiceError(
            "AR control account 1-1200 Trade Debtors is missing — "
            "re-run the CoA seed."
        )
    return acct


async def _get_unearned_income_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "2-1760",
            Account.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _get_gst_collected_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account | None:
    code = await settings_svc.get(session, "gst_collected_account_code", "")
    if not code:
        return None
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == str(code),
            Account.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _get_retentions_receivable_account(
    session: AsyncSession, company_id: uuid.UUID
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == "1-1220",
            Account.archived_at.is_(None),
        )
    )
    acct = result.scalar_one_or_none()
    if acct is None:
        raise InvoiceError(
            "Retentions Receivable account 1-1220 is missing — "
            "re-run the CoA seed or add account 1-1220 manually."
        )
    return acct


def _is_deferred_line(line: InvoiceLine) -> bool:
    """True when a line's service period spans more than one calendar month."""
    if line.service_start_date is None or line.service_end_date is None:
        return False
    s, e = line.service_start_date, line.service_end_date
    return (s.year, s.month) != (e.year, e.month)


async def post_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status == InvoiceStatus.POSTED:
        raise InvoiceError(f"Invoice {inv.id} is already posted")
    if inv.status == InvoiceStatus.VOIDED:
        raise InvoiceError(f"Invoice {inv.id} is voided; raise a new one")
    if not inv.lines:
        raise InvoiceError("Cannot post an invoice with no lines")
    if inv.total <= Decimal("0"):
        raise InvoiceError(f"Cannot post invoice with non-positive total {inv.total}")

    # Mint the invoice number now (DRAFT never burns a number).
    if not inv.number:
        inv.number = await numbering.next_number(
            session, inv.company_id, "invoice"
        )

    # ------------------------------------------------------------------ #
    # Cashbook mode: invoice is a document only, no JE on issue.
    # Income is recognised on payment (Dr Bank / Cr Income / Cr GST in
    # ``payments.post_payment``). Cash-basis sole-trader treatment.
    # Inventory / retention / deferred / trade-in are full-edition only.
    # ------------------------------------------------------------------ #
    from saebooks.services import edition as edition_svc
    if await edition_svc.is_cashbook_mode(session, inv.company_id):
        for line in inv.lines:
            if line.item_id is not None:
                raise InvoiceError(
                    "Cashbook-mode invoices cannot have inventory items. "
                    "Use a plain income-account line, or upgrade to full mode."
                )
            if getattr(line, "is_trade_in", False):
                raise InvoiceError(
                    "Cashbook-mode invoices cannot have trade-in lines. "
                    "Upgrade to full mode for trade-in accounting."
                )
            ret = getattr(line, "retention_pct", None)
            if ret is not None and Decimal(str(ret)) > Decimal("0"):
                raise InvoiceError(
                    "Cashbook-mode invoices cannot have retention. "
                    "Upgrade to full mode for retention accounting."
                )
        inv.status = InvoiceStatus.POSTED
        inv.posted_at = datetime.now(UTC)
        inv.posted_by = posted_by
        # journal_entry_id stays NULL — backfilled if/when the company
        # flips to full mode and the invoice is still open.
        await session.commit()
        return await get(session, inv.id)

    ar_account = await _get_ar_account(session, inv.company_id)

    # Deferred-revenue: look up Unearned Income (2-1760) and GST Collected
    # once per invoice so we don't hit the DB for every deferred line.
    unearned_acct = await _get_unearned_income_account(session, inv.company_id)
    gst_collected_acct = await _get_gst_collected_account(session, inv.company_id)

    # Post the journal in base currency. For AUD-only installs
    # ``fx_rate`` is 1 and base_* equal their unscaled counterparts, so
    # the math is identical to the pre-FX shape. For foreign-currency
    # invoices the per-line Cr amounts + GST are translated at the
    # invoice's rate.
    rate = Decimal(str(inv.fx_rate or Decimal("1")))

    # Separate trade-in lines from sale lines before any intermediate commit.
    # invoice.lines may not be re-accessed after bills_svc commits below
    # because async SQLAlchemy does not support implicit lazy loads.
    trade_in_lines = [line for line in inv.lines if line.is_trade_in]
    sale_lines = [line for line in inv.lines if not line.is_trade_in]

    # Calculate total retention amount across sale lines only (in base currency).
    # Retention is withheld from the ex-GST portion only — GST is always
    # charged on the full claim amount per ATO requirements.
    total_retention_base = sum(
        _q2(_q2(line.line_subtotal * Decimal(str(line.retention_pct))) / Decimal("100") * rate)
        for line in sale_lines
    )

    journal_lines: list[dict[str, object]] = []

    if total_retention_base > Decimal("0"):
        # Split Dr AR: Trade Debtors receives the net-payable portion
        # (ex-GST at 100%-retention + full GST); Retentions Receivable
        # receives the withheld ex-GST amount. Revenue recognised in full.
        retention_acct = await _get_retentions_receivable_account(session, inv.company_id)
        net_ar = _q2(inv.base_total - total_retention_base)
        journal_lines.append({
            "account_id": ar_account.id,
            "description": f"Invoice {inv.number} (net payable)",
            "debit": net_ar,
            "credit": Decimal("0"),
        })
        journal_lines.append({
            "account_id": retention_acct.id,
            "description": f"Invoice {inv.number}: retention held",
            "debit": total_retention_base,
            "credit": Decimal("0"),
        })
    else:
        # Standard path — no retention, single Dr Trade Debtors line.
        journal_lines.append({
            "account_id": ar_account.id,
            "description": f"Invoice {inv.number}",
            "debit": inv.base_total,
            "credit": Decimal("0"),
        })
    # One Cr line per income account per sale line; GST auto-poster
    # appends the matching Cr GST Collected. project_id is carried
    # through so the GL can drive P&L-by-project reports directly.
    # For item lines we also issue stock (which reads WAC) and append
    # the paired Dr COGS / Cr Inventory lines at WAC — the sale line
    # stays at sale price, the cost line moves at cost.
    # Deferred-revenue lines: Cr Unearned Income instead of the income
    # account; GST Collected is added explicitly because the auto-poster
    # only fires for INCOME/OTHER_INCOME account types, not LIABILITY.
    # Trade-in lines are excluded here — they are handled below.
    for line in sale_lines:
        line_base_subtotal = _q2(line.line_subtotal * rate)
        line_base_tax = (
            _q2(line.line_tax * rate) if line.line_tax > 0 else None
        )
        deferred = _is_deferred_line(line) and unearned_acct is not None
        if deferred:
            # Cr Unearned Income — no tax fields so auto-poster skips it
            journal_lines.append(
                {
                    "account_id": unearned_acct.id,  # type: ignore[union-attr]
                    "description": (
                        f"{inv.number}: {line.description} "
                        f"(deferred {line.service_start_date} – {line.service_end_date})"
                    ),
                    "debit": Decimal("0"),
                    "credit": line_base_subtotal,
                    "project_id": line.project_id,
                }
            )
            # Explicit Cr GST Collected (auto-poster won't fire for liability acct)
            if line_base_tax and gst_collected_acct is not None:
                journal_lines.append(
                    {
                        "account_id": gst_collected_acct.id,
                        "description": f"GST on {inv.number}: {line.description}",
                        "debit": Decimal("0"),
                        "credit": line_base_tax,
                    }
                )
        else:
            journal_lines.append(
                {
                    "account_id": line.account_id,
                    "description": f"{inv.number}: {line.description}",
                    "debit": Decimal("0"),
                    "credit": line_base_subtotal,
                    "tax_code_id": line.tax_code_id,
                    "gst_amount": line_base_tax,
                    "project_id": line.project_id,
                }
            )
        # Inventory: Dr COGS / Cr Inventory at WAC. issue_stock also
        # decrements on_hand_qty + raises if over-issuing. Runs inside
        # the same transaction as the journal post, so a raise here
        # rolls back both the stock mutation and the journal draft.
        if line.item_id is not None and line.quantity > Decimal("0"):
            item = await session.get(Item, line.item_id)
            if item is None:  # pragma: no cover — FK guarantees exists
                raise InvoiceError(f"Invoice line item {line.item_id} not found")
            cogs_value = await items_svc.issue_stock(
                session, line.item_id, qty=line.quantity
            )
            if cogs_value > Decimal("0"):
                cogs_value_2dp = _q2(cogs_value)
                journal_lines.append(
                    {
                        "account_id": item.cogs_account_id,
                        "description": f"{inv.number}: COGS {line.description}",
                        "debit": cogs_value_2dp,
                        "credit": Decimal("0"),
                        "project_id": line.project_id,
                    }
                )
                journal_lines.append(
                    {
                        "account_id": item.inventory_account_id,
                        "description": f"{inv.number}: stock out {line.description}",
                        "debit": Decimal("0"),
                        "credit": cogs_value_2dp,
                        "project_id": line.project_id,
                    }
                )

    # Auto-create and post a companion AP bill (Dr Inventory / Cr Trade
    # Creditors) for each trade-in line. This keeps the full new-car sale
    # in G1 and records the trade-in acquisition in AP/inventory with its
    # own independent GST treatment (MOTR-2).
    # bills_svc calls commit() internally; inv attributes set before this
    # loop are safe because they are scalar writes, not relationship reads.
    for tl in trade_in_lines:
        bill_draft = await bills_svc.create_draft(
            session,
            company_id=inv.company_id,
            contact_id=inv.contact_id,
            issue_date=inv.issue_date,
            due_date=inv.issue_date,
            supplier_reference=inv.number,
            notes=f"Trade-in acquisition for invoice {inv.number}",
            lines=[
                {
                    "description": tl.description,
                    "account_id": str(tl.account_id),
                    "tax_code_id": str(tl.tax_code_id) if tl.tax_code_id else None,
                    "quantity": str(tl.quantity),
                    "unit_price": str(tl.unit_price),
                    "discount_pct": str(tl.discount_pct),
                }
            ],
            currency=inv.currency,
            fx_rate=inv.fx_rate,
        )
        await bills_svc.post_bill(
            session,
            bill_draft.id,
            posted_by=posted_by,
            override_reason=override_reason,
        )

    # Use settlement_date as the GL entry date when set (RLES-6). Real
    # estate commissions are earned at unconditional exchange/settlement,
    # which is when BAS attribution should occur, not the issue date.
    gl_entry_date = inv.settlement_date if inv.settlement_date is not None else inv.issue_date
    entry = await journal_svc.create_draft(
        session,
        company_id=inv.company_id,
        tenant_id=inv.tenant_id,
        entry_date=gl_entry_date,
        description=f"Invoice {inv.number}",
        lines=journal_lines,
    )
    posted = await journal_svc.post(
        session, entry.id, posted_by=posted_by, override_reason=override_reason
    )

    inv.status = InvoiceStatus.POSTED
    inv.journal_entry_id = posted.id
    inv.posted_at = datetime.now(UTC)
    inv.posted_by = posted_by
    await session.commit()
    return await get(session, inv.id)


async def void_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    posted_by: str | None = None,
    override_reason: str | None = None,
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status == InvoiceStatus.VOIDED:
        return inv
    if inv.status == InvoiceStatus.DRAFT:
        inv.status = InvoiceStatus.VOIDED
        await session.commit()
        return inv
    if inv.amount_paid > Decimal("0"):
        raise InvoiceError(
            f"Invoice {inv.number} has payments allocated — "
            "unallocate before voiding."
        )
    if inv.journal_entry_id is None:
        # Cashbook-mode invoices have no JE on issue — just flip status.
        # (If a payment had landed, amount_paid > 0 would already have
        # blocked us above.)
        inv.status = InvoiceStatus.VOIDED
        await session.commit()
        return inv

    reversal = await journal_svc.reverse(
        session,
        inv.journal_entry_id,
        posted_by=posted_by,
        override_reason=override_reason or f"Void invoice {inv.number}",
        tenant_id=inv.tenant_id,
    )
    inv.status = InvoiceStatus.VOIDED
    inv.void_journal_entry_id = reversal.id
    await session.commit()
    return inv


async def mark_sent(
    session: AsyncSession, invoice_id: uuid.UUID
) -> Invoice:
    inv = await get(session, invoice_id)
    if inv.status != InvoiceStatus.POSTED:
        raise InvoiceError("Only POSTED invoices can be marked as sent")
    inv.sent_at = datetime.now(UTC)
    await session.commit()
    return inv


async def archive(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Invoice:
    inv = await get(session, invoice_id, tenant_id=tenant_id)
    inv.archived_at = datetime.now(UTC)
    await session.commit()
    return inv


# ==========================================================================
# API-oriented service (cycle 7) — optimistic locking + change_log
#
# These functions are the API surface for /api/v1/invoices.  They are
# intentionally separate from the legacy posting pipeline above so the
# two surfaces can evolve independently.
# ==========================================================================

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Invoice) -> None:
        super().__init__(
            f"Invoice {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Columns serialised into change_log.payload
# ---------------------------------------------------------------------------

_INV_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "number",
    "issue_date",
    "due_date",
    "settlement_date",
    "status",
    "subtotal",
    "tax_total",
    "total",
    "currency",
    "notes",
    "payment_terms",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise_invoice(inv: Invoice) -> dict:
    from decimal import Decimal as _D

    data: dict = {}
    for key in _INV_COLUMNS:
        val = getattr(inv, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, date):
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


async def _get_with_lines(
    session: AsyncSession, invoice_id: uuid.UUID
) -> Invoice | None:
    result = await session.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.one_off_customer))
        .where(Invoice.id == invoice_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    status: InvoiceStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Invoice], int]:
    """Return (invoices, total_count) — excludes archived invoices."""
    base_where = [
        Invoice.company_id == company_id,
        Invoice.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(Invoice.contact_id == contact_id)
    if status is not None:
        base_where.append(Invoice.status == status)
    if date_from is not None:
        base_where.append(Invoice.issue_date >= date_from)
    if date_to is not None:
        base_where.append(Invoice.issue_date <= date_to)

    count_stmt = select(func.count()).select_from(Invoice).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.one_off_customer))
        .where(*base_where)
        .order_by(Invoice.issue_date.desc(), Invoice.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    invoices = list((await session.execute(stmt)).scalars().unique().all())
    return invoices, total


async def api_get(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Invoice | None:
    """Fetch a single invoice with its lines. Returns None if not found.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant:
    a foreign-tenant id returns ``None`` even if the row exists. The
    parameter is keyword-only and optional so existing callers (the
    legacy posting pipeline, services that already filtered by company)
    keep working unchanged; the API layer always supplies it.

    P0 cross-tenant leak fix: the bare ``select(Invoice).where(id == id)``
    of the original implementation was an unscoped PK lookup — anyone
    who learned a foreign-tenant UUID via the leaky list endpoint could
    fetch the detail. With ``tenant_id`` supplied we now reject those
    lookups defensively, on top of the FORCE-RLS gate at the DB layer.
    """
    if tenant_id is None and company_id is None:
        return await _get_with_lines(session, invoice_id)
    clauses = [Invoice.id == invoice_id]
    if tenant_id is not None:
        clauses.append(Invoice.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Invoice.company_id == company_id)
    result = await session.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines), selectinload(Invoice.one_off_customer))
        .where(*clauses)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Cross-tenant FK validation (BKPR-1 P0 fix)
#
# Mirror of the CIVL-1 fix in services/bills.py. The invoice API must
# reject any contact_id, account_id, or tax_code_id that belongs to a
# different tenant before any INSERT or UPDATE. Raises InvoiceError with
# the message "<entity> not found in current tenant" so the router maps
# it to HTTP 422, matching the expected contract.
# ---------------------------------------------------------------------------


async def _validate_contact_company_and_tenant(
    session: AsyncSession,
    contact_id: uuid.UUID,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``InvoiceError`` if ``contact_id`` does not belong to ``tenant_id``
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
        raise InvoiceError(
            "contact_company_mismatch: contact does not belong to this company"
        )


async def _validate_account_tenant(
    session: AsyncSession,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``InvoiceError`` if ``account_id`` does not belong to ``tenant_id``."""
    result = await session.execute(
        select(Account.id).where(
            Account.id == account_id,
            Account.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise InvoiceError("account not found in current tenant")


async def _validate_tax_code_tenant(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Raise ``InvoiceError`` if ``tax_code_id`` does not belong to ``tenant_id``."""
    result = await session.execute(
        select(TaxCode.id).where(
            TaxCode.id == tax_code_id,
            TaxCode.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise InvoiceError("tax_code not found in current tenant")


async def _validate_line_fks(
    session: AsyncSession,
    lines: list[dict],
    tenant_id: uuid.UUID,
) -> None:
    """Validate every line's ``account_id`` + optional ``tax_code_id``.

    Each id must belong to ``tenant_id``; otherwise ``InvoiceError`` is raised.
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
    due_date: date,
    lines: list[dict] | None = None,
    reference: str | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
    settlement_date: date | None = None,
) -> Invoice:
    """Create an invoice draft with version=1 and a change_log row.

    BKPR-1 P0 fix: ``contact_id`` and every line's ``account_id`` /
    ``tax_code_id`` are validated against ``tenant_id`` before any
    INSERT. Cross-tenant FK injection raises ``InvoiceError`` (HTTP 422
    via the router).
    """
    await _validate_contact_company_and_tenant(session, contact_id, company_id, tenant_id)
    if lines:
        await _validate_line_fks(session, lines, tenant_id)

    locked_through = await journal_svc.get_locked_through(session, company_id)
    if locked_through is not None and issue_date <= locked_through:
        raise InvoiceError(
            f"Invoice date {issue_date} falls inside locked period "
            f"(ends {locked_through}); contact your controller to adjust period lock"
        )

    inv = Invoice(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        issue_date=issue_date,
        due_date=due_date,
        notes=notes,
        payment_terms=payment_terms,
        status=InvoiceStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
        version=1,
        settlement_date=settlement_date,
    )
    session.add(inv)
    await session.flush()
    await session.refresh(inv)

    if lines:
        await _replace_lines(session, inv, lines)
        await _recalc(session, inv)

    await session.flush()

    inv_loaded = await _get_with_lines(session, inv.id)
    assert inv_loaded is not None

    await change_log_svc.append(
        session,
        entity="invoice",
        entity_id=inv_loaded.id,
        op="create",
        actor=actor,
        payload=_serialise_invoice(inv_loaded),
        version=inv_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, inv_loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    notes: str | None = None,
    payment_terms: str | None = None,
    lines: list[dict] | None = None,
    settlement_date: date | None = None,
) -> Invoice:
    """Update an invoice draft with optimistic locking + change_log.

    BKPR-1 P0 fix: when ``contact_id`` or ``lines`` are supplied, every
    referenced contact / account / tax_code is validated against the
    invoice's owning ``tenant_id``. Cross-tenant FK injection raises
    ``InvoiceError`` (HTTP 422 via the router).
    """
    inv = await _get_with_lines(session, invoice_id)
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if inv.version != expected_version:
        raise VersionConflict(inv)
    if inv.status != InvoiceStatus.DRAFT:
        raise InvoiceError(
            f"invoice_not_draft: cannot edit invoice {inv.id} in state "
            f"{inv.status.value}; void the existing invoice and raise a new one instead."
        )

    if contact_id is not None:
        await _validate_contact_company_and_tenant(session, contact_id, inv.company_id, inv.tenant_id)
        inv.contact_id = contact_id
    if lines is not None:
        await _validate_line_fks(session, lines, inv.tenant_id)
    if issue_date is not None:
        inv.issue_date = issue_date
    if due_date is not None:
        inv.due_date = due_date
    if notes is not None:
        inv.notes = notes
    if payment_terms is not None:
        inv.payment_terms = payment_terms
    if settlement_date is not None:
        inv.settlement_date = settlement_date
    if lines is not None:
        await _replace_lines(session, inv, lines)
        await _recalc(session, inv)

    inv.version = inv.version + 1
    await session.flush()
    await session.refresh(inv)

    inv_loaded = await _get_with_lines(session, invoice_id)
    assert inv_loaded is not None

    await change_log_svc.append(
        session,
        entity="invoice",
        entity_id=inv_loaded.id,
        op="update",
        actor=actor,
        payload=_serialise_invoice(inv_loaded),
        version=inv_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, invoice_id)  # type: ignore[return-value]


async def api_void(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Invoice:
    """Soft-delete (archive/void) an invoice with optimistic locking + change_log."""
    inv = await _get_with_lines(session, invoice_id)
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if inv.version != expected_version:
        raise VersionConflict(inv)

    inv.archived_at = datetime.now(UTC)
    inv.status = InvoiceStatus.VOIDED
    inv.version = inv.version + 1
    await session.flush()
    await session.refresh(inv)

    inv_loaded = await _get_with_lines(session, invoice_id)
    assert inv_loaded is not None

    await change_log_svc.append(
        session,
        entity="invoice",
        entity_id=inv_loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise_invoice(inv_loaded),
        version=inv_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, invoice_id)  # type: ignore[return-value]


async def api_post_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Invoice:
    """Transition DRAFT → POSTED with JE generation, optimistic locking + change_log.

    Wraps the legacy ``post_invoice()`` pipeline which mints the invoice
    number, builds journal lines (Dr AR / Cr Income / Cr GST), calls
    ``journal_svc.post()``, and stamps ``journal_entry_id`` + ``posted_at``.
    After that legacy call completes and commits, we bump ``version`` and
    append a change_log row in a second transaction.

    When ``tenant_id`` is supplied the invoice must belong to that tenant;
    a mismatch raises ``InvoiceError("not found")`` so callers see a 404.
    """
    inv = await _get_with_lines(session, invoice_id)
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if tenant_id is not None and inv.tenant_id != tenant_id:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if inv.version != expected_version:
        raise VersionConflict(inv)
    if inv.status == InvoiceStatus.VOIDED:
        raise InvoiceError(
            f"Invoice {inv.id} is VOIDED and cannot be posted"
        )
    if inv.status == InvoiceStatus.POSTED:
        raise InvoiceError(f"Invoice {inv.id} is already POSTED")
    if not inv.lines:
        raise InvoiceError("Cannot post an invoice with no lines")

    # Delegate to the legacy pipeline (mints number, builds JE, posts it,
    # commits internally). After this call the session is in a fresh state.
    # PostingError (period lock, trust commingling, balance) is a legacy
    # exception type; translate it to InvoiceError so the router returns 422.
    try:
        inv = await post_invoice(
            session,
            invoice_id,
            posted_by=actor,
        )
    except journal_svc.PostingError as exc:
        raise InvoiceError(str(exc)) from exc

    # Bump version + append change_log in the same transaction.
    inv.version = inv.version + 1
    await session.flush()
    await session.refresh(inv)

    inv_loaded = await _get_with_lines(session, invoice_id)
    assert inv_loaded is not None

    await change_log_svc.append(
        session,
        entity="invoice",
        entity_id=inv_loaded.id,
        op="post",
        actor=actor,
        payload=_serialise_invoice(inv_loaded),
        version=inv_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, invoice_id)  # type: ignore[return-value]


async def api_void_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Invoice:
    """Transition any non-VOIDED → VOIDED with JE reversal (if POSTED),
    optimistic locking + change_log.

    Wraps the legacy ``void_invoice()`` pipeline which handles both the
    DRAFT case (no JE) and the POSTED case (reversal JE via
    ``journal_svc.reverse()``).

    When ``tenant_id`` is supplied the invoice must belong to that tenant;
    a mismatch raises ``InvoiceError("not found")`` so callers see a 404.
    """
    inv = await _get_with_lines(session, invoice_id)
    if inv is None:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if tenant_id is not None and inv.tenant_id != tenant_id:
        raise InvoiceError(f"Invoice {invoice_id} not found")
    if inv.version != expected_version:
        raise VersionConflict(inv)
    if inv.status == InvoiceStatus.VOIDED:
        raise InvoiceError(f"Invoice {inv.id} is already VOIDED")

    # Delegate to legacy pipeline (handles JE reversal where needed, commits).
    inv = await void_invoice(
        session,
        invoice_id,
        posted_by=actor,
        override_reason=f"API void by {actor}",
    )

    # Bump version + append change_log.
    inv.version = inv.version + 1
    await session.flush()
    await session.refresh(inv)

    inv_loaded = await _get_with_lines(session, invoice_id)
    assert inv_loaded is not None

    await change_log_svc.append(
        session,
        entity="invoice",
        entity_id=inv_loaded.id,
        op="void",
        actor=actor,
        payload=_serialise_invoice(inv_loaded),
        version=inv_loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, invoice_id)  # type: ignore[return-value]
