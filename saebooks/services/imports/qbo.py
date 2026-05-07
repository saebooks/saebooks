"""QuickBooks Online → SAE Books CSV migration importer.

Scope v1 (what ``parse_qbo_*`` actually handles):

* Contacts — parses QBO's standard "Customer.csv" / "Vendor.csv" /
  "Customers & vendors" export format. Maps Display Name, Company,
  Primary Email, Phone, ABN/Tax Reg Number + address fields.
* Chart of accounts — QBO's Account List CSV export. Maps Account
  Type + Detail Type to our ``AccountType``. Parent sub-accounts
  are carried through via "Parent" column when present.

Out of scope for v1 (stubbed, documented in ``docs/qbo_migration.md``):

* Open invoices / open bills — migrating A/R and A/P balances needs
  the user to confirm every number, which belongs in an interactive
  flow rather than a bulk import.
* Historical journal entries — QBO's "General Ledger" export is
  enormous and the user typically imports only open-balance cutovers.
* Classes, locations, projects — parking for Batch FF.

Design: pure parsers here, just like ``bank_csv``. Persistence for
contacts goes through ``contacts.create_contact`` (once lifted to a
service) or direct model inserts in the router. The CoA importer
reuses ``coa.apply_coa_diff`` — QBO's exported accounts are
normalised into ``CoaRow`` objects and fed to the same diff engine.
"""
from __future__ import annotations

import csv
import enum
import io
from collections.abc import Iterable
from dataclasses import dataclass

from saebooks.models.account import AccountType
from saebooks.models.contact import ContactType
from saebooks.services.imports.coa import CoaRow


class QboImportError(ValueError):
    """Raised when the QBO CSV can't be parsed."""


class QboContactKind(enum.StrEnum):
    """Which of QBO's two contact exports we're parsing."""

    CUSTOMER = "customer"
    VENDOR = "vendor"
    AUTO = "auto"


# --- Contact mapping -----------------------------------------------


@dataclass(frozen=True)
class QboContactRow:
    """One row from a QBO Customer/Vendor CSV export."""

    name: str
    contact_type: ContactType
    email: str | None
    phone: str | None
    abn: str | None
    address_line1: str | None
    city: str | None
    state: str | None
    postcode: str | None


# Canonical column names (lower-cased, stripped). QBO varies by region
# + subscription tier so we accept several synonyms per field.
_CONTACT_COL_MAP: dict[str, tuple[str, ...]] = {
    "name": ("customer", "vendor", "display name", "display name as"),
    "company": ("company", "company name"),
    "email": ("email", "primary email", "main email"),
    "phone": ("phone", "primary phone", "main phone", "work phone"),
    "abn": (
        "abn",
        "tax reg number",
        "tax number",
        "tax id",
        "tax id number",
        "business number",
    ),
    "billing_addr": ("billing address", "bill from", "address"),
    "billing_city": ("billing city", "city"),
    "billing_state": ("billing state", "state"),
    "billing_postcode": ("billing zip", "billing postal code", "postcode", "zip"),
}


def parse_qbo_contacts(
    raw: str,
    *,
    kind: QboContactKind = QboContactKind.AUTO,
) -> list[QboContactRow]:
    """Parse a QBO Customers / Vendors export CSV.

    ``kind`` sets the resulting ``ContactType`` on every row. ``AUTO``
    inspects the header — a column named ``Vendor`` / ``Supplier``
    biases SUPPLIER, anything else defaults CUSTOMER.
    """
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise QboImportError("CSV has no header row")

    lowered = {f.lower().strip(): f for f in reader.fieldnames}

    resolved_kind = kind
    if kind is QboContactKind.AUTO:
        if any(k in lowered for k in ("vendor", "supplier")):
            resolved_kind = QboContactKind.VENDOR
        else:
            resolved_kind = QboContactKind.CUSTOMER

    name_key = _pick(lowered, _CONTACT_COL_MAP["name"])
    company_key = _pick(lowered, _CONTACT_COL_MAP["company"])
    if not name_key and not company_key:
        raise QboImportError("could not find a name/company column")
    email_key = _pick(lowered, _CONTACT_COL_MAP["email"])
    phone_key = _pick(lowered, _CONTACT_COL_MAP["phone"])
    abn_key = _pick(lowered, _CONTACT_COL_MAP["abn"])
    addr_key = _pick(lowered, _CONTACT_COL_MAP["billing_addr"])
    city_key = _pick(lowered, _CONTACT_COL_MAP["billing_city"])
    state_key = _pick(lowered, _CONTACT_COL_MAP["billing_state"])
    postcode_key = _pick(lowered, _CONTACT_COL_MAP["billing_postcode"])

    contact_type = (
        ContactType.SUPPLIER
        if resolved_kind is QboContactKind.VENDOR
        else ContactType.CUSTOMER
    )

    out: list[QboContactRow] = []
    for r in reader:
        name = _first_nonempty(r, name_key, company_key)
        if not name:
            continue
        out.append(
            QboContactRow(
                name=name,
                contact_type=contact_type,
                email=_nullable(r.get(email_key, "") if email_key else ""),
                phone=_nullable(r.get(phone_key, "") if phone_key else ""),
                abn=_nullable(r.get(abn_key, "") if abn_key else ""),
                address_line1=_nullable(
                    r.get(addr_key, "") if addr_key else ""
                ),
                city=_nullable(r.get(city_key, "") if city_key else ""),
                state=_nullable(r.get(state_key, "") if state_key else ""),
                postcode=_nullable(
                    r.get(postcode_key, "") if postcode_key else ""
                ),
            )
        )
    return out


# --- CoA mapping ---------------------------------------------------


@dataclass(frozen=True)
class QboCoaRow:
    """Lightweight shape for a parsed QBO account row.

    This is the raw form — ``to_coa_rows()`` normalises into the
    standard ``CoaRow`` consumed by the diff engine.
    """

    code: str | None  # QBO "Account Number"
    name: str
    account_type: AccountType
    parent: str | None


_QBO_TYPE_MAP: dict[str, AccountType] = {
    # QBO's Top-level types → ours
    "bank": AccountType.ASSET,
    "accounts receivable": AccountType.ASSET,
    "other current assets": AccountType.ASSET,
    "other current asset": AccountType.ASSET,
    "fixed assets": AccountType.ASSET,
    "fixed asset": AccountType.ASSET,
    "other assets": AccountType.ASSET,
    "other asset": AccountType.ASSET,
    "accounts payable": AccountType.LIABILITY,
    "credit card": AccountType.LIABILITY,
    "other current liabilities": AccountType.LIABILITY,
    "other current liability": AccountType.LIABILITY,
    "long term liabilities": AccountType.LIABILITY,
    "long term liability": AccountType.LIABILITY,
    "equity": AccountType.EQUITY,
    "income": AccountType.INCOME,
    "other income": AccountType.OTHER_INCOME,
    "cost of goods sold": AccountType.COST_OF_SALES,
    "expenses": AccountType.EXPENSE,
    "expense": AccountType.EXPENSE,
    "other expense": AccountType.OTHER_EXPENSE,
}


def parse_qbo_accounts(raw: str) -> list[QboCoaRow]:
    """Parse a QBO Account List CSV export."""
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise QboImportError("CSV has no header row")

    lowered = {f.lower().strip(): f for f in reader.fieldnames}

    name_key = _pick(lowered, ("account", "name", "account name"))
    type_key = _pick(lowered, ("type", "account type"))
    if not name_key or not type_key:
        raise QboImportError(
            "QBO accounts CSV must include 'Account' and 'Type' columns"
        )
    number_key = _pick(lowered, ("num", "number", "account number"))
    parent_key = _pick(lowered, ("parent", "parent account", "sub-account of"))

    rows: list[QboCoaRow] = []
    for r in reader:
        name = (r.get(name_key, "") or "").strip()
        if not name:
            continue
        qbo_type = (r.get(type_key, "") or "").strip().lower()
        mapped = _QBO_TYPE_MAP.get(qbo_type)
        if mapped is None:
            # Unknown — skip rather than explode. Helpful error
            # message: list what we do recognise in the exception.
            raise QboImportError(
                f"account {name!r}: unknown QBO type {qbo_type!r}"
            )
        rows.append(
            QboCoaRow(
                code=_nullable(r.get(number_key, "") if number_key else ""),
                name=name,
                account_type=mapped,
                parent=_nullable(r.get(parent_key, "") if parent_key else ""),
            )
        )
    return rows


def qbo_coa_to_rows(
    rows: Iterable[QboCoaRow], *, default_code_prefix: str = "9"
) -> list[CoaRow]:
    """Convert QBO rows to the canonical ``CoaRow`` shape.

    Rows without an Account Number get a synthesised code of the form
    ``<prefix>-<5-digit incrementing>``. Nine is our reserved "imported"
    namespace so they sort to the bottom of the CoA.
    """
    out: list[CoaRow] = []
    next_synth = 1
    for r in rows:
        code = r.code
        if not code:
            code = f"{default_code_prefix}-{next_synth:05d}"
            next_synth += 1
        out.append(
            CoaRow(
                code=code,
                name=r.name,
                account_type=r.account_type,
                parent_code=None,  # parent names aren't codes; left to caller
                tax_code_default=None,
                reconcile=r.account_type is AccountType.ASSET,
            )
        )
    return out


# --- helpers -------------------------------------------------------


def _pick(lowered: dict[str, str], names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in lowered:
            return lowered[n]
    return None


def _nullable(value: str) -> str | None:
    v = (value or "").strip()
    return v or None


def _first_nonempty(
    row: dict[str, str], *keys: str | None
) -> str:
    for key in keys:
        if key is None:
            continue
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


__all__ = [
    "QboCoaRow",
    "QboContactKind",
    "QboContactRow",
    "QboImportError",
    "parse_qbo_accounts",
    "parse_qbo_contacts",
    "qbo_coa_to_rows",
]
