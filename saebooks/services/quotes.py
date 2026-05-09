"""Quote service — pre-invoice sales document with no GL impact.

Lifecycle
---------

    DRAFT  →  SENT  →  ACCEPTED  →  INVOICED   (terminal)
                    ↘  DECLINED                  (terminal)
    (any non-terminal) →  ARCHIVED              (terminal)

Edits
-----
* DRAFT and SENT can be edited.
* ACCEPTED / DECLINED / ARCHIVED / INVOICED are read-only.

Convert-to-invoice
------------------
``convert_to_invoice`` mints a DRAFT invoice copying the quote's
customer / currency / lines (description, quantity, unit_price,
tax_code_id, account_id), sets invoice.terms from quote.terms,
invoice.notes from quote.notes, and stamps ``invoiced_at`` /
``invoice_id`` / ``source_quote_id`` atomically.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.contact import Contact
from saebooks.models.quote import Quote, QuoteLine, QuoteStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import change_log as change_log_svc
from saebooks.services import numbering

_TWOPLACES = Decimal("0.01")
_FOURPLACES = Decimal("0.0001")
_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class QuoteError(ValueError):
    """Raised on quote validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Quote) -> None:
        super().__init__(
            f"Quote {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------- #
# Math helpers                                                            #
# ---------------------------------------------------------------------- #


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_FOURPLACES, rounding=ROUND_HALF_UP)


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


@dataclass(frozen=True)
class _LineInput:
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_code_id: uuid.UUID | None
    account_id: uuid.UUID | None


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
        raise QuoteError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


# ---------------------------------------------------------------------- #
# Cross-tenant FK validation                                              #
# ---------------------------------------------------------------------- #


async def _validate_customer_tenant(
    session: AsyncSession,
    customer_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(Contact.id).where(
            Contact.id == customer_id,
            Contact.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise QuoteError("customer not found in current tenant")


async def _validate_tax_code_tenant(
    session: AsyncSession,
    tax_code_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(TaxCode.id).where(
            TaxCode.id == tax_code_id,
            TaxCode.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise QuoteError("tax_code not found in current tenant")


# ---------------------------------------------------------------------- #
# Line replacement + recalc                                               #
# ---------------------------------------------------------------------- #


async def _replace_lines(
    session: AsyncSession,
    quote: Quote,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Hard-replace quote lines."""
    await session.execute(
        sa_delete(QuoteLine).where(QuoteLine.quote_id == quote.id)
    )
    await session.flush()
    session.expire(quote, ["lines"])

    for i, raw in enumerate(lines, 1):
        tax_code_id = raw.get("tax_code_id")
        if isinstance(tax_code_id, str) and tax_code_id:
            tax_code_id = uuid.UUID(tax_code_id)
        elif not tax_code_id:
            tax_code_id = None

        account_id = raw.get("account_id")
        if isinstance(account_id, str) and account_id:
            account_id = uuid.UUID(account_id)
        elif not account_id:
            account_id = None

        if tax_code_id is not None and tenant_id is not None:
            await _validate_tax_code_tenant(session, _as_uuid(tax_code_id), tenant_id)

        line_input = _LineInput(
            description=str(raw["description"]),
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            account_id=account_id if isinstance(account_id, uuid.UUID) else None,
        )

        tax_rate = await _resolve_tax_rate(session, line_input.tax_code_id, company_id)
        gross = line_input.quantity * line_input.unit_price
        subtotal = _q2(gross)
        tax = _q2(subtotal * tax_rate / Decimal("100"))
        line_total = subtotal + tax

        session.add(
            QuoteLine(
                quote_id=quote.id,
                line_no=i,
                description=line_input.description,
                quantity=line_input.quantity,
                unit_price=line_input.unit_price,
                tax_code_id=line_input.tax_code_id,
                line_total=line_total,
                account_id=line_input.account_id,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, quote: Quote) -> None:
    lines = (
        await session.execute(
            select(QuoteLine).where(QuoteLine.quote_id == quote.id)
        )
    ).scalars().all()

    # We store line_total (which already includes tax). Derive subtotal
    # from unit_price * quantity; tax = line_total - subtotal.
    subtotal = sum((ln.quantity * ln.unit_price for ln in lines), Decimal("0"))
    total = sum((ln.line_total for ln in lines), Decimal("0"))
    tax = total - subtotal

    quote.subtotal = _q2(Decimal(str(subtotal)))
    quote.tax_total = _q2(Decimal(str(tax)))
    quote.total = _q2(Decimal(str(total)))


# ---------------------------------------------------------------------- #
# Serialisation for change_log                                            #
# ---------------------------------------------------------------------- #

_QUOTE_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "customer_id",
    "number",
    "issue_date",
    "expiry_date",
    "status",
    "subtotal",
    "tax_total",
    "total",
    "currency",
    "validity_days",
    "deposit_pct",
    "late_fee_pct_per_month",
    "is_supply_only",
    "notes",
    "terms",
    "accepted_at",
    "declined_at",
    "invoiced_at",
    "invoice_id",
    "version",
    "created_at",
    "updated_at",
)


def _serialise(quote: Quote) -> dict:
    data: dict = {}
    for key in _QUOTE_COLUMNS:
        val = getattr(quote, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, date):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------- #
# Read operations                                                         #
# ---------------------------------------------------------------------- #


async def _get_with_lines(
    session: AsyncSession,
    quote_id: uuid.UUID,
) -> Quote | None:
    result = await session.execute(
        select(Quote)
        .options(selectinload(Quote.lines))
        .where(Quote.id == quote_id)
    )
    return result.scalar_one_or_none()


async def api_get(
    session: AsyncSession,
    quote_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Quote | None:
    """Fetch a quote with lines. Returns None if not found / wrong tenant."""
    if tenant_id is None:
        return await _get_with_lines(session, quote_id)
    result = await session.execute(
        select(Quote)
        .options(selectinload(Quote.lines))
        .where(
            Quote.id == quote_id,
            Quote.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    customer_id: uuid.UUID | None = None,
    status: QuoteStatus | None = None,
    since: date | None = None,
    expiry_before: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Quote], int]:
    """Return (quotes, total_count) — excludes items with no archived_at field
    (quotes don't soft-archive like invoices; ARCHIVED status is the equivalent).
    """
    base_where = [
        Quote.company_id == company_id,
        Quote.tenant_id == tenant_id,
    ]
    if customer_id is not None:
        base_where.append(Quote.customer_id == customer_id)
    if status is not None:
        base_where.append(Quote.status == status)
    if since is not None:
        base_where.append(Quote.issue_date >= since)
    if expiry_before is not None:
        base_where.append(Quote.expiry_date <= expiry_before)

    count_stmt = select(func.count()).select_from(Quote).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Quote)
        .options(selectinload(Quote.lines))
        .where(*base_where)
        .order_by(Quote.issue_date.desc(), Quote.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().unique().all())
    return rows, total


# ---------------------------------------------------------------------- #
# Write operations                                                        #
# ---------------------------------------------------------------------- #


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    customer_id: uuid.UUID,
    issue_date: date,
    expiry_date: date | None = None,
    lines: list[dict] | None = None,
    notes: str | None = None,
    terms: str | None = None,
    currency: str = "AUD",
    validity_days: int = 28,
    deposit_pct: Decimal | None = None,
    late_fee_pct_per_month: Decimal | None = None,
    is_supply_only: bool = False,
) -> Quote:
    """Create a DRAFT quote with version=1 + change_log row."""
    await _validate_customer_tenant(session, customer_id, tenant_id)

    # Compute expiry from validity_days if not supplied
    if expiry_date is None:
        from datetime import timedelta
        expiry_date = issue_date + timedelta(days=validity_days)

    quote = Quote(
        company_id=company_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        issue_date=issue_date,
        expiry_date=expiry_date,
        notes=notes,
        terms=terms,
        status=QuoteStatus.DRAFT,
        currency=currency.upper(),
        validity_days=validity_days,
        deposit_pct=deposit_pct if deposit_pct is not None else Decimal("50"),
        late_fee_pct_per_month=(
            late_fee_pct_per_month
            if late_fee_pct_per_month is not None
            else Decimal("2.5")
        ),
        is_supply_only=is_supply_only,
        version=1,
    )
    session.add(quote)
    await session.flush()
    await session.refresh(quote)

    if lines:
        await _replace_lines(
            session, quote, lines, company_id=company_id, tenant_id=tenant_id
        )
        await _recalc(session, quote)

    await session.flush()
    loaded = await _get_with_lines(session, quote.id)
    assert loaded is not None

    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="create",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, loaded.id)  # type: ignore[return-value]


async def api_update(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    customer_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    expiry_date: date | None = None,
    notes: str | None = None,
    terms: str | None = None,
    currency: str | None = None,
    validity_days: int | None = None,
    deposit_pct: Decimal | None = None,
    late_fee_pct_per_month: Decimal | None = None,
    is_supply_only: bool | None = None,
    lines: list[dict] | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Quote:
    """Update a DRAFT or SENT quote.

    ACCEPTED / DECLINED / ARCHIVED / INVOICED are read-only.
    """
    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status in (
        QuoteStatus.ACCEPTED,
        QuoteStatus.DECLINED,
        QuoteStatus.ARCHIVED,
        QuoteStatus.INVOICED,
    ):
        raise QuoteError(
            f"Quote {quote.id} is {quote.status.value} and cannot be edited"
        )

    if customer_id is not None:
        await _validate_customer_tenant(session, customer_id, quote.tenant_id)
        quote.customer_id = customer_id
    if issue_date is not None:
        quote.issue_date = issue_date
    if expiry_date is not None:
        quote.expiry_date = expiry_date
    if notes is not None:
        quote.notes = notes
    if terms is not None:
        quote.terms = terms
    if currency is not None:
        quote.currency = currency.upper()
    if validity_days is not None:
        quote.validity_days = validity_days
    if deposit_pct is not None:
        quote.deposit_pct = deposit_pct
    if late_fee_pct_per_month is not None:
        quote.late_fee_pct_per_month = late_fee_pct_per_month
    if is_supply_only is not None:
        quote.is_supply_only = is_supply_only
    if lines is not None:
        await _replace_lines(
            session,
            quote,
            lines,
            company_id=quote.company_id,
            tenant_id=quote.tenant_id,
        )
        await _recalc(session, quote)

    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None

    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, quote_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# State transitions                                                       #
# ---------------------------------------------------------------------- #


async def api_send(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Quote:
    """DRAFT → SENT. Mints the quote number."""
    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status != QuoteStatus.DRAFT:
        raise QuoteError(
            f"Quote {quote.id} is {quote.status.value}, expected DRAFT"
        )

    if not quote.number:
        quote.number = await numbering.next_number(
            session, quote.company_id, "quote"
        )
    quote.status = QuoteStatus.SENT
    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, quote_id)  # type: ignore[return-value]


async def api_accept(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Quote:
    """SENT → ACCEPTED. Stamps accepted_at."""
    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status != QuoteStatus.SENT:
        raise QuoteError(
            f"Quote {quote.id} is {quote.status.value}; accept requires SENT"
        )

    quote.status = QuoteStatus.ACCEPTED
    quote.accepted_at = datetime.now(UTC)
    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, quote_id)  # type: ignore[return-value]


async def api_decline(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Quote:
    """SENT → DECLINED. Stamps declined_at."""
    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status != QuoteStatus.SENT:
        raise QuoteError(
            f"Quote {quote.id} is {quote.status.value}; decline requires SENT"
        )

    quote.status = QuoteStatus.DECLINED
    quote.declined_at = datetime.now(UTC)
    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, quote_id)  # type: ignore[return-value]


async def api_archive(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Quote:
    """Any non-INVOICED → ARCHIVED."""
    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status == QuoteStatus.INVOICED:
        raise QuoteError(
            f"Quote {quote.id} is INVOICED and cannot be archived"
        )
    if quote.status == QuoteStatus.ARCHIVED:
        raise QuoteError(f"Quote {quote.id} is already ARCHIVED")

    quote.status = QuoteStatus.ARCHIVED
    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, quote_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# Convert-to-invoice                                                      #
# ---------------------------------------------------------------------- #


async def convert_to_invoice(
    session: AsyncSession,
    quote_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> tuple[Quote, object]:
    """ACCEPTED → INVOICED. Mints a DRAFT invoice from the quote's lines.

    The resulting invoice carries:
    * contact_id = quote.customer_id
    * notes = quote.notes
    * payment_terms = quote.terms
    * source_quote_id = quote.id (audit trail)
    * lines mapped from quote_lines preserving description, quantity,
      unit_price, tax_code_id, account_id

    The quote flips to INVOICED and quote.invoice_id + quote.invoiced_at
    are stamped.

    Returns (quote, invoice) — invoice is the newly-minted DRAFT Invoice
    ORM object (already committed).
    """
    from saebooks.services import invoices as inv_svc

    quote = await _get_with_lines(session, quote_id)
    if quote is None:
        raise QuoteError(f"Quote {quote_id} not found")
    if tenant_id is not None and quote.tenant_id != tenant_id:
        raise QuoteError(f"Quote {quote_id} not found")
    if quote.version != expected_version:
        raise VersionConflict(quote)
    if quote.status != QuoteStatus.ACCEPTED:
        raise QuoteError(
            f"Quote {quote.id} is {quote.status.value}; convert-to-invoice requires ACCEPTED"
        )

    # Hard-fail if any line is missing account_id — invoice lines require it.
    missing = [ln.line_no for ln in quote.lines if ln.account_id is None]
    if missing:
        line_list = ", ".join(str(n) for n in missing)
        raise QuoteError(
            f"Cannot convert to invoice: line(s) {line_list} are missing account_id. "
            "Set an account on every line before converting."
        )

    # Build invoice lines from quote lines
    invoice_lines: list[dict] = [
        {
            "description": ln.description,
            "quantity": ln.quantity,
            "unit_price": ln.unit_price,
            "tax_code_id": ln.tax_code_id,
            "account_id": ln.account_id,
            "discount_pct": Decimal("0"),
        }
        for ln in quote.lines
    ]

    today = date.today()
    inv = await inv_svc.api_create(
        session,
        quote.company_id,
        quote.tenant_id,
        actor=actor,
        contact_id=quote.customer_id,
        issue_date=today,
        due_date=today,
        lines=invoice_lines if invoice_lines else None,
        notes=quote.notes,
        payment_terms=quote.terms,
        currency=quote.currency,
    )

    # Stamp source_quote_id on the invoice (column added in 0097)
    from saebooks.db import AsyncSessionLocal as _ASL  # noqa: F401
    from sqlalchemy import update as sa_update
    from saebooks.models.invoice import Invoice

    await session.execute(
        sa_update(Invoice)
        .where(Invoice.id == inv.id)
        .values(source_quote_id=quote.id)
    )
    await session.flush()

    # Re-fetch quote (inv_svc.api_create commits + starts new transaction
    # via the service pattern — we need a fresh handle on the quote)
    quote = await _get_with_lines(session, quote_id)
    assert quote is not None

    quote.status = QuoteStatus.INVOICED
    quote.invoiced_at = datetime.now(UTC)
    quote.invoice_id = inv.id
    quote.version = quote.version + 1
    await session.flush()
    await session.refresh(quote)

    loaded = await _get_with_lines(session, quote_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="quote",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload={
            **_serialise(loaded),
            "convert_to_invoice": {"invoice_id": str(inv.id)},
        },
        version=loaded.version,
    )
    await session.commit()
    return (
        await _get_with_lines(session, quote_id),  # type: ignore[return-value]
        inv,
    )
