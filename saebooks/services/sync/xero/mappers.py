"""SAE Books <-> Xero shape conversions.

Pure functions — no DB access, no HTTP. Inputs are dicts (Xero shape)
or ORM rows (SAE Books shape); outputs are the inverse.

Why not Pydantic
----------------
Xero's shape is loose: optional fields appear or not, the same field
sometimes carries an empty string and sometimes is omitted. Building a
Pydantic model that captures every Xero behaviour means modelling
Xero's bugs. We use plain dicts and validate inline so a missing field
turns into ``None``, not a ``ValidationError`` at the boundary.

The pull-direction mappers return dataclasses — typed, but tolerant
of partial input. They do not write to the DB; the orchestrator in
``pull.py`` upserts using these dataclasses as input.

The push-direction mappers take an ORM row and return a Xero-shaped
dict ready to send to ``endpoints.post_*``.

Invoice immutability
--------------------
Per ``[[feedback_saebooks-marketing-differentiator]]``, posted invoices
are append-only on the SAE Books side: the rendered PDF snapshot is
the source of truth for "what we sent the customer". The push-direction
invoice mapper preserves this by NEVER mapping an invoice's line
descriptions back from Xero on conflict — only header fields (status,
due-date, amount-paid) update from Xero. Line edits in Xero would
break the snapshot invariant; conflict-resolution UI surfaces them
explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceLine, InvoiceStatus

# ---------------------------------------------------------------------- #
# Helpers                                                                #
# ---------------------------------------------------------------------- #


def _parse_xero_date(value: str | None) -> date | None:
    """Parse a Xero date.

    Xero serialises dates as either ``YYYY-MM-DDT00:00:00`` (no offset)
    OR the legacy MS-AJAX ``/Date(1234567890+0000)/`` form. We accept
    both.
    """
    if not value:
        return None
    if value.startswith("/Date("):
        # /Date(1234567890+0000)/  ->  1234567890
        inner = value[6:-2]
        # strip optional timezone suffix
        for sep in ("+", "-"):
            idx = inner.find(sep, 1)
            if idx > 0:
                inner = inner[:idx]
                break
        try:
            ms = int(inner)
        except ValueError:
            return None
        return datetime.utcfromtimestamp(ms / 1000.0).date()
    # ISO-ish
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _format_xero_date(d: date | None) -> str | None:
    """Format a SAE Books date for Xero (ISO 8601 date, no time)."""
    if d is None:
        return None
    return d.isoformat()


def _decimal(value: object, *, default: str = "0") -> Decimal:
    """Tolerant Decimal parser — accepts str, int, float, Decimal, None."""
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return Decimal(default)


# ---------------------------------------------------------------------- #
# Contacts                                                               #
# ---------------------------------------------------------------------- #


@dataclass
class XeroContactPull:
    """Shape produced by ``xero_contact_to_saebooks``.

    The orchestrator upserts a ``Contact`` row from this. ``contact_type``
    is inferred from ``IsCustomer`` / ``IsSupplier`` flags.
    """

    external_id: str
    external_etag: str | None
    name: str
    contact_type: ContactType
    email: str | None
    phone: str | None
    abn: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postcode: str | None
    country: str | None
    archived: bool


def xero_contact_to_saebooks(row: dict[str, Any]) -> XeroContactPull:
    """Map one Xero ``Contact`` dict to a SAE Books-ready ``XeroContactPull``.

    Xero shape (relevant subset)::

        {
          "ContactID": "<guid>",
          "Name": "Acme Pty Ltd",
          "EmailAddress": "ap@acme.example",
          "Phones": [{"PhoneType":"DEFAULT", "PhoneNumber":"+61 7 ..."}],
          "Addresses": [{"AddressType":"STREET", ...}, {"AddressType":"POBOX", ...}],
          "TaxNumber": "12 345 678 901",   # ABN in AU
          "IsCustomer": true,
          "IsSupplier": false,
          "ContactStatus": "ACTIVE" | "ARCHIVED" | "GDPRREQUEST",
          "UpdatedDateUTC": "/Date(1700000000000+0000)/",
        }
    """
    is_customer = bool(row.get("IsCustomer"))
    is_supplier = bool(row.get("IsSupplier"))
    if is_customer and is_supplier:
        contact_type = ContactType.BOTH
    elif is_supplier:
        contact_type = ContactType.SUPPLIER
    else:
        # Default to customer when neither flag is set (Xero allows
        # contacts that are neither — typically prospects). Treating
        # them as customers matches our default behaviour.
        contact_type = ContactType.CUSTOMER

    phones = row.get("Phones") or []
    default_phone = next(
        (p.get("PhoneNumber") for p in phones if p.get("PhoneType") == "DEFAULT"),
        None,
    )
    # Concatenate area + number when present (Xero splits them).
    if default_phone:
        phone_obj: dict[str, Any] = next(
            (p for p in phones if p.get("PhoneType") == "DEFAULT"),
            {},
        )
        area = phone_obj.get("PhoneAreaCode") or ""
        country_code = phone_obj.get("PhoneCountryCode") or ""
        if area or country_code:
            default_phone = (
                f"{country_code} {area} {phone_obj.get('PhoneNumber', '')}"
                .strip()
            )

    addresses = row.get("Addresses") or []
    street = next(
        (a for a in addresses if a.get("AddressType") == "STREET"),
        None,
    )
    if street is None and addresses:
        street = addresses[0]
    street = street or {}

    return XeroContactPull(
        external_id=row["ContactID"],
        external_etag=row.get("UpdatedDateUTC"),
        name=row.get("Name") or "(unnamed)",
        contact_type=contact_type,
        email=row.get("EmailAddress") or None,
        phone=default_phone,
        abn=row.get("TaxNumber"),
        address_line1=street.get("AddressLine1") or None,
        address_line2=street.get("AddressLine2") or None,
        city=street.get("City") or None,
        state=(street.get("Region") or "")[:8] or None,
        postcode=street.get("PostalCode") or None,
        country=street.get("Country") or None,
        archived=row.get("ContactStatus") == "ARCHIVED",
    )


def saebooks_contact_to_xero(contact: Contact) -> dict[str, Any]:
    """Map a SAE Books ``Contact`` ORM row to a Xero ``Contact`` dict.

    Idempotent: if the contact already has an ``external_id`` for Xero,
    the dict carries ``ContactID`` so Xero treats it as an update.
    Otherwise the dict has no ``ContactID`` and Xero creates it.
    """
    is_customer = contact.contact_type in (ContactType.CUSTOMER, ContactType.BOTH)
    is_supplier = contact.contact_type in (ContactType.SUPPLIER, ContactType.BOTH)
    out: dict[str, Any] = {
        "Name": contact.name,
        "IsCustomer": is_customer,
        "IsSupplier": is_supplier,
    }
    if contact.external_source == "xero" and contact.external_id:
        out["ContactID"] = contact.external_id
    if contact.email:
        out["EmailAddress"] = contact.email
    if contact.abn:
        out["TaxNumber"] = contact.abn
    if contact.phone:
        out["Phones"] = [
            {
                "PhoneType": "DEFAULT",
                "PhoneNumber": contact.phone,
            }
        ]
    address: dict[str, str] = {"AddressType": "STREET"}
    has_address = False
    if contact.address_line1:
        address["AddressLine1"] = contact.address_line1
        has_address = True
    if contact.address_line2:
        address["AddressLine2"] = contact.address_line2
        has_address = True
    if contact.city:
        address["City"] = contact.city
        has_address = True
    if contact.state:
        address["Region"] = contact.state
        has_address = True
    if contact.postcode:
        address["PostalCode"] = contact.postcode
        has_address = True
    if contact.country:
        address["Country"] = contact.country
        has_address = True
    if has_address:
        out["Addresses"] = [address]
    if contact.archived_at is not None:
        out["ContactStatus"] = "ARCHIVED"
    return out


# ---------------------------------------------------------------------- #
# Invoices                                                                #
# ---------------------------------------------------------------------- #


# Status mapping. Xero -> SAE Books. We never demote (per plan §1):
# Xero AUTHORISED -> SAE Books POSTED, but if SAE Books is already
# POSTED and Xero says DRAFT, we leave SAE Books alone.
_XERO_TO_SAEBOOKS_INVOICE_STATUS: dict[str, InvoiceStatus] = {
    "DRAFT": InvoiceStatus.DRAFT,
    "SUBMITTED": InvoiceStatus.DRAFT,  # awaiting approval
    "AUTHORISED": InvoiceStatus.POSTED,
    "PAID": InvoiceStatus.POSTED,  # status is "POSTED + paid"; amount_paid carries the rest
    "VOIDED": InvoiceStatus.VOIDED,
    "DELETED": InvoiceStatus.VOIDED,  # treat hard-deleted in Xero as voided here
}

_SAEBOOKS_TO_XERO_INVOICE_STATUS: dict[InvoiceStatus, str] = {
    InvoiceStatus.DRAFT: "DRAFT",
    InvoiceStatus.POSTED: "AUTHORISED",
    InvoiceStatus.VOIDED: "VOIDED",
}


@dataclass
class XeroInvoiceLinePull:
    description: str
    quantity: Decimal
    unit_amount: Decimal
    line_amount: Decimal
    tax_amount: Decimal
    account_code: str | None
    tax_type: str | None
    item_code: str | None


@dataclass
class XeroInvoicePull:
    external_id: str
    external_etag: str | None
    invoice_type: str  # "ACCREC" | "ACCPAY"
    number: str | None
    contact_external_id: str | None
    contact_name: str | None
    issue_date: date | None
    due_date: date | None
    status: InvoiceStatus
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    amount_paid: Decimal
    currency: str
    fx_rate: Decimal
    lines: list[XeroInvoiceLinePull] = field(default_factory=list)


def xero_invoice_to_saebooks(row: dict[str, Any]) -> XeroInvoicePull:
    """Map one Xero ``Invoice`` dict to a SAE Books-ready dataclass."""
    contact = row.get("Contact") or {}
    raw_status = row.get("Status") or "DRAFT"
    status = _XERO_TO_SAEBOOKS_INVOICE_STATUS.get(raw_status, InvoiceStatus.DRAFT)

    raw_lines = row.get("LineItems") or []
    lines = [
        XeroInvoiceLinePull(
            description=ln.get("Description") or "",
            quantity=_decimal(ln.get("Quantity"), default="1"),
            unit_amount=_decimal(ln.get("UnitAmount")),
            line_amount=_decimal(ln.get("LineAmount")),
            tax_amount=_decimal(ln.get("TaxAmount")),
            account_code=ln.get("AccountCode"),
            tax_type=ln.get("TaxType"),
            item_code=ln.get("ItemCode"),
        )
        for ln in raw_lines
    ]

    return XeroInvoicePull(
        external_id=row["InvoiceID"],
        external_etag=row.get("UpdatedDateUTC"),
        invoice_type=row.get("Type") or "ACCREC",
        number=row.get("InvoiceNumber") or None,
        contact_external_id=contact.get("ContactID"),
        contact_name=contact.get("Name"),
        issue_date=_parse_xero_date(row.get("Date")),
        due_date=_parse_xero_date(row.get("DueDate")),
        status=status,
        subtotal=_decimal(row.get("SubTotal")),
        tax_total=_decimal(row.get("TotalTax")),
        total=_decimal(row.get("Total")),
        amount_paid=_decimal(row.get("AmountPaid")),
        currency=row.get("CurrencyCode") or "AUD",
        fx_rate=_decimal(row.get("CurrencyRate"), default="1"),
        lines=lines,
    )


def saebooks_invoice_to_xero(
    invoice: Invoice,
    *,
    lines: list[InvoiceLine] | None = None,
    contact_external_id: str | None = None,
) -> dict[str, Any]:
    """Map a SAE Books ``Invoice`` (header + lines) to a Xero ``Invoice`` dict.

    ``contact_external_id`` is the Xero ``ContactID`` of the related
    contact — the orchestrator looks this up via ``sync_state`` and
    passes it in. If ``None``, we send only the contact name and let
    Xero match by name (lossy — falls back to creating a new contact;
    the orchestrator should always look up first).

    ``lines`` defaults to ``invoice.lines``. Pass an explicit list when
    the orchestrator has eager-loaded them under a separate session.
    """
    if lines is None:
        lines = list(invoice.lines)

    out: dict[str, Any] = {
        "Type": "ACCREC",  # AR; bill push uses ACCPAY in a separate mapper
        "Status": _SAEBOOKS_TO_XERO_INVOICE_STATUS[invoice.status],
        "Date": _format_xero_date(invoice.issue_date),
        "DueDate": _format_xero_date(invoice.due_date),
        "CurrencyCode": invoice.currency or "AUD",
        "LineAmountTypes": "Exclusive",  # SAE Books lines are tax-exclusive (see model docstring)
        "LineItems": [
            _line_to_xero(ln) for ln in lines
        ],
    }
    if invoice.number:
        out["InvoiceNumber"] = invoice.number
    if invoice.external_source == "xero" and invoice.external_id:
        out["InvoiceID"] = invoice.external_id
    if contact_external_id:
        out["Contact"] = {"ContactID": contact_external_id}
    elif invoice.contact_id:
        # Fallback — caller didn't resolve the link. Xero will best-
        # effort match on name, which the orchestrator can detect
        # afterwards and patch up.
        out["Contact"] = {"ContactID": str(invoice.contact_id)}
    if invoice.fx_rate and invoice.currency != "AUD":
        out["CurrencyRate"] = str(invoice.fx_rate)
    return out


def _line_to_xero(ln: InvoiceLine) -> dict[str, Any]:
    """Map one ``InvoiceLine`` row to a Xero ``LineItem``."""
    out: dict[str, Any] = {
        "Description": ln.description,
        "Quantity": str(ln.quantity),
        "UnitAmount": str(ln.unit_price),
    }
    # Discount in Xero is expressed as a percent on the line.
    if ln.discount_pct and ln.discount_pct != Decimal("0"):
        out["DiscountRate"] = str(ln.discount_pct)
    return out


# ---------------------------------------------------------------------- #
# Manual journals (push-only)                                            #
# ---------------------------------------------------------------------- #


def saebooks_journal_to_xero(
    *,
    narration: str,
    journal_date: date,
    lines: list[dict[str, Any]],
    status: str = "POSTED",
) -> dict[str, Any]:
    """Build a Xero ``ManualJournal`` dict.

    ``lines`` is a list of ``{account_code, description, amount, tax_type?}``;
    by Xero convention positive amounts are debits, negative are credits.
    The caller (push.py) translates SAE Books JournalLine debit/credit
    into signed amounts.

    ``status`` is "POSTED" by default — Xero allows "DRAFT" for
    operator review, but pushing drafts is rarely useful (the operator
    has already authorised the journal on our side).
    """
    return {
        "Narration": narration,
        "Date": _format_xero_date(journal_date),
        "Status": status,
        "JournalLines": [
            {
                "AccountCode": ln["account_code"],
                "Description": ln.get("description") or "",
                "LineAmount": str(ln["amount"]),
                **(
                    {"TaxType": ln["tax_type"]}
                    if ln.get("tax_type")
                    else {}
                ),
            }
            for ln in lines
        ],
    }


__all__ = [
    "XeroContactPull",
    "XeroInvoiceLinePull",
    "XeroInvoicePull",
    "saebooks_contact_to_xero",
    "saebooks_invoice_to_xero",
    "saebooks_journal_to_xero",
    "xero_contact_to_saebooks",
    "xero_invoice_to_saebooks",
]
