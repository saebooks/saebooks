"""Purchase Order service — commitment document with no GL impact.

Lifecycle
---------

    DRAFT  →  OPEN  →  PARTIAL ─→ RECEIVED ─→ CLOSED
                  ↘   CANCELLED  (terminal, distinct from CLOSED)

Edits
-----
* DRAFT can be edited freely.
* OPEN can be edited (revisions). Each edit bumps ``version`` and
  appends a ``change_log`` row so revisions are auditable.
* PARTIAL can be edited only on the unreceived remainder per line —
  the user MAY add lines or change the unreceived portion, but the
  service refuses to drop ``quantity`` below ``received_qty``.
* RECEIVED / CLOSED / CANCELLED are read-only.

Convert-to-bill
---------------
``convert_to_bill`` mints a DRAFT bill carrying the PO's contact /
currency / fx_rate / lines (each line's quantity becomes
``quantity - received_qty``), advances ``received_qty`` on every PO
line, and links the bill back to the PO via ``Bill.purchase_order_id``
when that column exists. (We don't add the column here — purely
optional surface; bills service ignores the back-link if missing.)

The default convert is "all unreceived". A future overload can take
a per-line ``quantity_to_bill`` map for partial multi-receipt; the
service is structured so that path is a one-line addition.

After convert, status auto-flips:
* ``received_qty == quantity`` for every line  → RECEIVED
* otherwise (some lines short of full)         → PARTIAL

GL impact
---------
None at the PO layer — see the model docstring.
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

from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.contact import Contact
from saebooks.models.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from saebooks.models.tax_code import TaxCode
from saebooks.services import change_log as change_log_svc
from saebooks.services import idempotency as idem_svc
from saebooks.services import numbering
from saebooks.services import preaccounting_client as _preacct
from saebooks.services import preaccounting_facades as _pf
from saebooks.services.idempotency import ClaimStatus

_TWOPLACES = Decimal("0.01")
_FOURPLACES = Decimal("0.0001")
_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class PurchaseOrderError(ValueError):
    """Raised on PO validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: PurchaseOrder) -> None:
        super().__init__(
            f"PurchaseOrder {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------- #
# Math helpers — same shape as bills/invoices (add-on / ex-GST tax)      #
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
    account_id: uuid.UUID
    tax_code_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    project_id: uuid.UUID | None
    item_id: uuid.UUID | None


def _compute_line_totals(
    line: _LineInput, tax_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (subtotal, tax, total) — add-on (ex-GST) tax treatment."""
    gross = line.quantity * line.unit_price
    discount_factor = (Decimal("100") - line.discount_pct) / Decimal("100")
    subtotal = _q2(gross * discount_factor)
    tax = _q2(subtotal * tax_rate / Decimal("100"))
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
        raise PurchaseOrderError(f"tax_code {tax_code_id} not found")
    return Decimal(str(tc.rate or 0))


# ---------------------------------------------------------------------- #
# Cross-tenant FK validation (mirrors services/bills.py)                  #
# ---------------------------------------------------------------------- #


async def _validate_contact_tenant(
    session: AsyncSession,
    contact_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(Contact.id).where(
            Contact.id == contact_id,
            Contact.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise PurchaseOrderError("contact not found in current tenant")


async def _validate_account_tenant(
    session: AsyncSession,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    result = await session.execute(
        select(Account.id).where(
            Account.id == account_id,
            Account.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise PurchaseOrderError("account not found in current tenant")


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
        raise PurchaseOrderError("tax_code not found in current tenant")


async def _validate_line_fks(
    session: AsyncSession,
    lines: list[dict],
    tenant_id: uuid.UUID,
) -> None:
    for raw in lines:
        account_raw = raw.get("account_id")
        if account_raw is not None:
            await _validate_account_tenant(
                session, _as_uuid(account_raw), tenant_id
            )
        tax_code_raw = raw.get("tax_code_id")
        if tax_code_raw:
            await _validate_tax_code_tenant(
                session, _as_uuid(tax_code_raw), tenant_id
            )


# ---------------------------------------------------------------------- #
# Line replacement + recalc                                               #
# ---------------------------------------------------------------------- #


async def _replace_lines(
    session: AsyncSession,
    po: PurchaseOrder,
    lines: list[dict[str, object]],
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    """Hard-replace lines. Caller is responsible for state-machine checks
    that protect ``received_qty`` integrity (see ``api_update``)."""
    await session.execute(
        sa_delete(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == po.id
        )
    )
    await session.flush()
    session.expire(po, ["lines"])

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
        if company_id is not None:
            chk = await session.execute(
                select(Account.id).where(
                    Account.id == account_id, Account.company_id == company_id
                )
            )
            if chk.scalar_one_or_none() is None:
                raise PurchaseOrderError(f"account {account_id} not found")

        line_input = _LineInput(
            description=str(raw["description"]),
            account_id=account_id,
            tax_code_id=tax_code_id if isinstance(tax_code_id, uuid.UUID) else None,
            quantity=Decimal(str(raw.get("quantity", 1))),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            discount_pct=Decimal(str(raw.get("discount_pct", 0))),
            project_id=project_id if isinstance(project_id, uuid.UUID) else None,
            item_id=item_id if isinstance(item_id, uuid.UUID) else None,
        )
        tax_rate = await _resolve_tax_rate(
            session, line_input.tax_code_id, company_id
        )
        subtotal, tax, total = _compute_line_totals(line_input, tax_rate)

        # Optional ``received_qty`` from caller (multi-receipt re-edits).
        # Default 0 on a fresh line; honoured only if non-negative and not
        # exceeding the new quantity. The service-layer guard in
        # ``api_update`` enforces the cross-line invariant.
        received_raw = raw.get("received_qty", 0)
        received_qty = Decimal(str(received_raw or 0))
        if received_qty < Decimal("0"):
            raise PurchaseOrderError(
                f"received_qty must be >= 0 (got {received_qty})"
            )
        if received_qty > line_input.quantity:
            raise PurchaseOrderError(
                f"received_qty {received_qty} exceeds quantity {line_input.quantity}"
            )

        session.add(
            PurchaseOrderLine(
                purchase_order_id=po.id,
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
                received_qty=received_qty,
                project_id=line_input.project_id,
                item_id=line_input.item_id,
            )
        )
    await session.flush()


async def _recalc(session: AsyncSession, po: PurchaseOrder) -> None:
    lines = (
        await session.execute(
            select(PurchaseOrderLine).where(
                PurchaseOrderLine.purchase_order_id == po.id
            )
        )
    ).scalars().all()
    subtotal = sum((ln.line_subtotal for ln in lines), Decimal("0"))
    tax = sum((ln.line_tax for ln in lines), Decimal("0"))
    po.subtotal = _q2(Decimal(subtotal))
    po.tax_total = _q2(Decimal(tax))
    po.total = po.subtotal + po.tax_total

    rate = Decimal(str(po.fx_rate or Decimal("1")))
    base_subtotal = sum(
        (_q2(ln.line_subtotal * rate) for ln in lines), Decimal("0")
    )
    base_tax = sum((_q2(ln.line_tax * rate) for ln in lines), Decimal("0"))
    po.base_subtotal = _q2(Decimal(base_subtotal))
    po.base_tax_total = _q2(Decimal(base_tax))
    po.base_total = po.base_subtotal + po.base_tax_total


# ---------------------------------------------------------------------- #
# Serialisation for change_log                                            #
# ---------------------------------------------------------------------- #

_PO_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "contact_id",
    "number",
    "issue_date",
    "expected_date",
    "status",
    "subtotal",
    "tax_total",
    "total",
    "currency",
    "fx_rate",
    "delivery_address",
    "notes",
    "sent_at",
    "closed_at",
    "cancelled_at",
    "version",
    "created_at",
    "updated_at",
    "archived_at",
)


def _serialise(po: PurchaseOrder) -> dict:
    data: dict = {}
    for key in _PO_COLUMNS:
        val = getattr(po, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, (datetime, date)):
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
    po_id: uuid.UUID,
) -> PurchaseOrder | None:
    result = await session.execute(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(PurchaseOrder.id == po_id)
    )
    return result.scalar_one_or_none()


async def api_get(
    session: AsyncSession,
    po_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> PurchaseOrder | None:
    """Fetch a PO with lines. Returns ``None`` if not found / wrong tenant."""
    if _preacct.delegating():
        return await _pf.po_get(po_id, tenant_id, company_id)
    if tenant_id is None and company_id is None:
        return await _get_with_lines(session, po_id)
    clauses = [PurchaseOrder.id == po_id]
    if tenant_id is not None:
        clauses.append(PurchaseOrder.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(PurchaseOrder.company_id == company_id)
    result = await session.execute(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(*clauses)
    )
    return result.scalar_one_or_none()


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    status: PurchaseOrderStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PurchaseOrder], int]:
    """Return (purchase_orders, total_count) — excludes archived rows."""
    if _preacct.delegating():
        return await _pf.po_list(
            company_id,
            tenant_id,
            contact_id=contact_id,
            status=status.value if status is not None else None,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    base_where = [
        PurchaseOrder.company_id == company_id,
        PurchaseOrder.archived_at.is_(None),
    ]
    if contact_id is not None:
        base_where.append(PurchaseOrder.contact_id == contact_id)
    if status is not None:
        base_where.append(PurchaseOrder.status == status)
    if date_from is not None:
        base_where.append(PurchaseOrder.issue_date >= date_from)
    if date_to is not None:
        base_where.append(PurchaseOrder.issue_date <= date_to)

    count_stmt = select(func.count()).select_from(PurchaseOrder).where(*base_where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(*base_where)
        .order_by(PurchaseOrder.issue_date.desc(), PurchaseOrder.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().unique().all())
    return rows, total


# ---------------------------------------------------------------------- #
# Write operations — DRAFT lifecycle                                      #
# ---------------------------------------------------------------------- #


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    contact_id: uuid.UUID,
    issue_date: date,
    expected_date: date | None = None,
    delivery_address: str | None = None,
    lines: list[dict] | None = None,
    notes: str | None = None,
    currency: str = "AUD",
    fx_rate: Decimal | None = None,
) -> PurchaseOrder:
    """Create a DRAFT purchase order with ``version=1`` + ``change_log``.

    ``contact_id`` and every line's ``account_id`` / ``tax_code_id`` are
    validated against ``tenant_id`` before INSERT — same belt-and-braces
    pattern as bills, since RLS bypass is silent for owner-role connections.
    """
    if _preacct.delegating():
        return await _pf.po_create(
            company_id,
            tenant_id,
            actor,
            contact_id=contact_id,
            issue_date=issue_date,
            expected_date=expected_date,
            delivery_address=delivery_address,
            lines=lines,
            notes=notes,
            currency=currency,
            fx_rate=fx_rate,
        )
    await _validate_contact_tenant(session, contact_id, tenant_id)
    if lines:
        await _validate_line_fks(session, lines, tenant_id)

    po = PurchaseOrder(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        issue_date=issue_date,
        expected_date=expected_date,
        delivery_address=delivery_address,
        notes=notes,
        status=PurchaseOrderStatus.DRAFT,
        currency=currency.upper(),
        fx_rate=fx_rate if fx_rate is not None else Decimal("1"),
        version=1,
    )
    session.add(po)
    await session.flush()
    await session.refresh(po)

    if lines:
        await _replace_lines(session, po, lines, company_id=company_id)
        await _recalc(session, po)

    await session.flush()
    loaded = await _get_with_lines(session, po.id)
    assert loaded is not None

    await change_log_svc.append(
        session,
        entity="purchase_order",
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
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    force: bool = False,
    *,
    contact_id: uuid.UUID | None = None,
    issue_date: date | None = None,
    expected_date: date | None = None,
    delivery_address: str | None = None,
    notes: str | None = None,
    currency: str | None = None,
    fx_rate: Decimal | None = None,
    lines: list[dict] | None = None,
    tenant_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """Update a DRAFT or OPEN/PARTIAL purchase order.

    Constraints
    -----------
    * RECEIVED / CLOSED / CANCELLED are read-only — raises ``PurchaseOrderError``.
    * On OPEN/PARTIAL, replacement lines must keep ``received_qty`` <= new
      ``quantity`` per line; lines with ``received_qty > 0`` MUST appear
      in the replacement set with at least the received amount preserved.
      Adding new lines is fine. Removing a line that has been received
      from is rejected (would orphan a received quantity that has been
      billed).
    """
    if _preacct.delegating():
        return await _pf.po_update(
            po_id,
            actor,
            expected_version,
            force,
            tenant_id,
            contact_id=contact_id,
            issue_date=issue_date,
            expected_date=expected_date,
            delivery_address=delivery_address,
            notes=notes,
            currency=currency,
            fx_rate=fx_rate,
            lines=lines,
        )
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)
    if not force and po.status in (
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.CLOSED,
        PurchaseOrderStatus.CANCELLED,
    ):
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} is {po.status.value} and cannot be edited"
        )

    if contact_id is not None:
        await _validate_contact_tenant(session, contact_id, po.tenant_id)
        po.contact_id = contact_id
    if lines is not None:
        await _validate_line_fks(session, lines, po.tenant_id)

        # Multi-receipt safety: when the PO is OPEN/PARTIAL and any line
        # has ``received_qty > 0``, the caller must not drop that line
        # or its received portion. We enforce this by requiring the
        # caller to round-trip ``received_qty`` on every preserved line
        # (the API echoes it back in the GET response). If a caller
        # forgets to send received_qty on a partially-received line we
        # refuse rather than silently zeroing the receipt history.
        if po.status in (
            PurchaseOrderStatus.OPEN,
            PurchaseOrderStatus.PARTIAL,
        ):
            already = {
                ln.line_no: ln.received_qty for ln in po.lines if ln.received_qty > 0
            }
            if already:
                # Build a {line_no -> received_qty} from the incoming
                # lines (1-indexed by position). If a line that was
                # previously partially received is now missing or has
                # received_qty < the prior amount, refuse.
                for i, raw in enumerate(lines, 1):
                    prior = already.get(i)
                    if prior is None:
                        continue
                    incoming = Decimal(str(raw.get("received_qty", 0) or 0))
                    if incoming < prior:
                        raise PurchaseOrderError(
                            f"line {i}: received_qty was {prior}, "
                            f"replacement carries {incoming} — multi-receipt "
                            "history must be preserved (re-fetch the PO and "
                            "round-trip received_qty on every preserved line)"
                        )
                # Refuse if a previously-received line index is dropped.
                if len(lines) < max(already):
                    raise PurchaseOrderError(
                        "a previously-received line was removed; "
                        "multi-receipt history must be preserved"
                    )

    if issue_date is not None:
        po.issue_date = issue_date
    if expected_date is not None:
        po.expected_date = expected_date
    if delivery_address is not None:
        po.delivery_address = delivery_address
    if notes is not None:
        po.notes = notes
    if currency is not None:
        po.currency = currency.upper()
    if fx_rate is not None:
        po.fx_rate = fx_rate
    if lines is not None:
        await _replace_lines(session, po, lines, company_id=po.company_id)
        await _recalc(session, po)
    elif fx_rate is not None:
        await _recalc(session, po)

    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None

    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# State transitions                                                       #
# ---------------------------------------------------------------------- #


async def api_send(
    session: AsyncSession,
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """DRAFT → OPEN. Mints the PO number, stamps ``sent_at``."""
    if _preacct.delegating():
        return await _pf.po_transition("send", po_id, actor, expected_version, tenant_id)
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)
    if po.status != PurchaseOrderStatus.DRAFT:
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} is {po.status.value}, expected DRAFT"
        )
    if not po.lines:
        raise PurchaseOrderError("Cannot send a PO with no lines")
    if po.total <= Decimal("0"):
        raise PurchaseOrderError(
            f"Cannot send PO with non-positive total {po.total}"
        )

    if not po.number:
        po.number = await numbering.next_number(
            session, po.company_id, "purchase_order"
        )
    po.status = PurchaseOrderStatus.OPEN
    po.sent_at = datetime.now(UTC)
    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


async def api_cancel(
    session: AsyncSession,
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """Any non-terminal → CANCELLED. Refuses if any line has been received."""
    if _preacct.delegating():
        return await _pf.po_transition("cancel", po_id, actor, expected_version, tenant_id)
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)
    if po.status in (
        PurchaseOrderStatus.CLOSED,
        PurchaseOrderStatus.CANCELLED,
    ):
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} is already {po.status.value}"
        )
    if any(ln.received_qty > Decimal("0") for ln in po.lines):
        raise PurchaseOrderError(
            "Cannot cancel a PO with received lines — close it instead"
        )

    po.status = PurchaseOrderStatus.CANCELLED
    po.cancelled_at = datetime.now(UTC)
    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


async def api_close(
    session: AsyncSession,
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """RECEIVED or PARTIAL → CLOSED. PARTIAL closes the unreceived
    remainder (caller has decided not to chase it)."""
    if _preacct.delegating():
        return await _pf.po_transition("close", po_id, actor, expected_version, tenant_id)
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)
    if po.status not in (
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.PARTIAL,
        PurchaseOrderStatus.OPEN,
    ):
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} is {po.status.value}; close requires "
            "OPEN, PARTIAL, or RECEIVED"
        )

    po.status = PurchaseOrderStatus.CLOSED
    po.closed_at = datetime.now(UTC)
    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


async def api_archive(
    session: AsyncSession,
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """Soft-delete (sets ``archived_at``)."""
    if _preacct.delegating():
        return await _pf.po_transition("archive", po_id, actor, expected_version, tenant_id)
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)

    po.archived_at = datetime.now(UTC)
    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="archive",
        actor=actor,
        payload=_serialise(loaded),
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
# Convert-to-bill                                                         #
# ---------------------------------------------------------------------- #


def _po_convert_key(
    po_id: uuid.UUID,
    received_before: dict[str, str],
    billed: dict[str, str],
) -> tuple[str, dict]:
    """Idempotency key + body for one PO→bill receipt batch (runbook §1.7).

    ``key = hash(po_id, receipt-batch identity)``. The batch identity is
    derived from exactly what the code uses to decide quantities: the
    per-line ``received_qty`` snapshot BEFORE this batch (the receipt-batch
    sequence discriminator — it advances after every committed batch) plus
    the resolved per-line quantities being billed. So a crash-retry (state
    unchanged) reuses the key and REPLAYs the same bill, while a genuinely
    later receipt batch (received_qty already advanced) gets a fresh key and
    legitimately creates a second bill — even if it bills identical
    quantities.
    """
    identity = {
        "po_id": str(po_id),
        "received_before": received_before,
        "billed": billed,
    }
    digest = idem_svc.canonical_body_sha256(identity)[:32]
    return f"po2bill:{po_id}:{digest}", identity


async def _ensure_bill_fact(
    session: AsyncSession,
    po: PurchaseOrder,
    *,
    key: str,
    body: dict,
    bill_lines_data: list[dict],
    issue: date,
    due: date,
    supplier_reference: str | None,
    actor: str,
) -> Bill:
    """Phase 1 (fact) — create, or idempotently replay, the DRAFT bill.

    Goes through the PUBLIC ``bills_svc.api_create`` path (runbook §1.3) — the
    same ``_replace_lines`` / ``_recalc`` helpers the old inline build used, so
    the DRAFT bill is byte-for-byte equivalent (supplier_reference, lines,
    ``notes="From PO {number}"``) but now with the standard bill change_log +
    cross-tenant FK validation api_create provides. The bill, the idempotency
    claim and the stored identity commit atomically (``commit=False``), so the
    hand-off has no in-flight window.
    """
    from saebooks.services import bills as bills_svc

    slot = await idem_svc.claim_fact(
        session, key=key, tenant_id=po.tenant_id, body=body
    )

    if slot.status is ClaimStatus.REPLAY:
        bill_id = uuid.UUID(slot.cached["bill_id"])
        bill = await bills_svc.api_get(session, bill_id, tenant_id=po.tenant_id)
        assert bill is not None
        return bill

    if slot.status is ClaimStatus.CONFLICT:
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} convert-to-bill idempotency conflict for "
            "this receipt batch"
        )

    if slot.status is ClaimStatus.IN_FLIGHT:
        # The atomic claim+fact+response commit removes this window in-process;
        # kept as a defensive, retriable signal.
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} convert-to-bill is mid-flight; retry shortly"
        )

    # CLAIMED — first run. Build the bill without committing so the claim, the
    # bill and the stored response commit atomically below.
    bill = await bills_svc.api_create(
        session,
        po.company_id,
        po.tenant_id,
        actor=actor,
        contact_id=po.contact_id,
        issue_date=issue,
        due_date=due,
        lines=bill_lines_data,
        reference=supplier_reference,
        notes=f"From PO {po.number or po.id}",
        currency=po.currency,
        fx_rate=po.fx_rate,
        commit=False,
    )
    await idem_svc.record_fact(session, key=key, identity={"bill_id": str(bill.id)})
    await session.commit()
    bill = await bills_svc.api_get(session, bill.id, tenant_id=po.tenant_id)
    assert bill is not None
    return bill


async def _apply_po_advance(
    session: AsyncSession,
    po_id: uuid.UUID,
    advances: dict[uuid.UUID, Decimal],
    bill: Bill,
    actor: str,
) -> PurchaseOrder:
    """Phase 2 (module state) — advance received_qty + flip PO status.

    Committed after the bill fact. Idempotent guard: if a prior run already
    recorded this bill against the PO (change_log), skip re-advancing so a
    retry after a crash between the two commits does not double-count.
    """
    po = await _get_with_lines(session, po_id)
    assert po is not None

    # If the PO's change_log already names this bill, phase 2 already ran for
    # this batch — do not advance received_qty a second time.
    if await _po_convert_already_recorded(session, po_id, bill.id):
        return po

    for ln in po.lines:
        if ln.id in advances:
            ln.received_qty = ln.received_qty + advances[ln.id]

    fully_received = all(ln.received_qty >= ln.quantity for ln in po.lines)
    po.status = (
        PurchaseOrderStatus.RECEIVED
        if fully_received
        else PurchaseOrderStatus.PARTIAL
    )
    po.version = po.version + 1
    await session.flush()
    await session.refresh(po)

    loaded = await _get_with_lines(session, po_id)
    assert loaded is not None
    await change_log_svc.append(
        session,
        entity="purchase_order",
        entity_id=loaded.id,
        op="update",
        actor=actor,
        payload={
            **_serialise(loaded),
            "convert_to_bill": {
                "bill_id": str(bill.id),
                "advances": {str(k): str(v) for k, v in advances.items()},
            },
        },
        version=loaded.version,
    )
    await session.commit()
    return await _get_with_lines(session, po_id)  # type: ignore[return-value]


async def _po_convert_already_recorded(
    session: AsyncSession,
    po_id: uuid.UUID,
    bill_id: uuid.UUID,
) -> bool:
    """True if a PO change_log row already links ``po_id`` → ``bill_id``.

    The recovery guard for the phase-1/phase-2 crash window: it detects a
    completed phase-2 advance for this exact bill so a retry is a no-op.
    """
    from saebooks.models.change_log import ChangeLog

    rows = (
        await session.execute(
            select(ChangeLog.payload).where(
                ChangeLog.entity == "purchase_order",
                ChangeLog.entity_id == po_id,
            )
        )
    ).scalars().all()
    target = str(bill_id)
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        conv = payload.get("convert_to_bill")
        if isinstance(conv, dict) and conv.get("bill_id") == target:
            return True
    return False


async def convert_to_bill(
    session: AsyncSession,
    po_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    tenant_id: uuid.UUID | None = None,
    quantities: dict[int, Decimal] | None = None,
    bill_issue_date: date | None = None,
    bill_due_date: date | None = None,
    supplier_reference: str | None = None,
) -> tuple[PurchaseOrder, Bill]:
    """Mint a DRAFT bill from the PO's outstanding lines.

    Default behaviour (``quantities=None``)
        Bills the full unreceived quantity on every line.

    Partial mode (``quantities={line_no: qty, ...}``)
        Bills the specified quantity per line. Each value must be in
        ``[0, quantity - received_qty]``. ``0`` skips the line. Lines
        omitted from the dict are skipped.

    State change on the PO
        Each line's ``received_qty`` advances by the billed quantity.
        Status auto-flips:

        * every line fully received → RECEIVED
        * at least one received but not all → PARTIAL
        * none of the lines received (all skipped) → unchanged, raises

    Two-phase, fact-first (runbook §§1.3, 1.7)
        The DRAFT bill is created + committed FIRST via the public
        ``bills_svc.api_create`` path under an idempotency key =
        ``hash(po_id, receipt-batch identity)``, THEN the PO's ``received_qty``
        advance + status flip commit second. A crash between the two is
        recoverable: re-running the SAME receipt batch REPLAYs the same bill
        (no double-create) and completes the advance, while a genuinely later
        batch gets a fresh key and creates a second bill. The atomicity the old
        single-transaction inline build gave up across the (future) HTTP
        boundary is restored by the idempotency key. The bill remains DRAFT;
        the caller posts it via ``/api/v1/bills/{id}/post`` when the supplier
        invoice arrives.
    """
    if _preacct.delegating():
        return await _pf.po_convert_to_bill(
            po_id,
            actor,
            expected_version,
            tenant_id,
            quantities=quantities,
            bill_issue_date=bill_issue_date,
            bill_due_date=bill_due_date,
            supplier_reference=supplier_reference,
        )
    po = await _get_with_lines(session, po_id)
    if po is None:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if tenant_id is not None and po.tenant_id != tenant_id:
        raise PurchaseOrderError(f"PurchaseOrder {po_id} not found")
    if po.version != expected_version:
        raise VersionConflict(po)
    if po.status not in (
        PurchaseOrderStatus.OPEN,
        PurchaseOrderStatus.PARTIAL,
    ):
        raise PurchaseOrderError(
            f"PurchaseOrder {po.id} is {po.status.value}; convert "
            "requires OPEN or PARTIAL"
        )

    # Resolve per-line quantities to bill. Capture the received_qty snapshot
    # BEFORE any advance — it is the receipt-batch discriminator in the key.
    received_before: dict[str, str] = {
        str(ln.line_no): str(ln.received_qty) for ln in po.lines
    }
    bill_lines_data: list[dict] = []
    advances: dict[uuid.UUID, Decimal] = {}
    billed: dict[str, str] = {}
    for ln in po.lines:
        outstanding = ln.quantity - ln.received_qty
        if outstanding < Decimal("0"):
            outstanding = Decimal("0")
        if quantities is None:
            qty = outstanding
        else:
            qty = Decimal(str(quantities.get(ln.line_no, 0) or 0))
            if qty < Decimal("0"):
                raise PurchaseOrderError(
                    f"line {ln.line_no}: quantity to bill must be >= 0 "
                    f"(got {qty})"
                )
            if qty > outstanding:
                raise PurchaseOrderError(
                    f"line {ln.line_no}: only {outstanding} outstanding, "
                    f"cannot bill {qty}"
                )
        if qty <= Decimal("0"):
            continue
        bill_lines_data.append(
            {
                "line_no": ln.line_no,
                "description": ln.description,
                "account_id": ln.account_id,
                "tax_code_id": ln.tax_code_id,
                "quantity": qty,
                "unit_price": ln.unit_price,
                "discount_pct": ln.discount_pct,
                "project_id": ln.project_id,
                "item_id": ln.item_id,
            }
        )
        advances[ln.id] = qty
        billed[str(ln.line_no)] = str(qty)

    if not bill_lines_data:
        raise PurchaseOrderError(
            "Nothing to bill — every line is fully received or skipped"
        )

    issue = bill_issue_date or date.today()
    due = bill_due_date or issue
    key, body = _po_convert_key(po.id, received_before, billed)

    bill = await _ensure_bill_fact(
        session,
        po,
        key=key,
        body=body,
        bill_lines_data=bill_lines_data,
        issue=issue,
        due=due,
        supplier_reference=supplier_reference,
        actor=actor,
    )
    po_final = await _apply_po_advance(session, po_id, advances, bill, actor)
    return po_final, bill
