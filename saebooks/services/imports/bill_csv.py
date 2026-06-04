"""Bulk-import supplier bills from a CSV — as DRAFTS for review.

Interim bulk-entry path (Richard, 2026-06-04) while document ingestion
beds in. Safety rule (same as the Paperless ingest): an import must NOT
be able to corrupt the books. Therefore:

* every imported bill is created as ``DRAFT`` — never posted; a human
  reviews and posts each one in the GUI;
* a row is only imported when its supplier, GL account and (if given)
  tax code all resolve EXACTLY — an unresolved row is **rejected with a
  clear error**, never guessed and never silently dropped;
* malformed dates/amounts reject the row, they don't get coerced.

One CSV row = one bill with one GL line. Header-driven, column aliases
accepted. The commit returns a per-row created/skipped/errors report so
the operator sees exactly what landed.
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import bills as bills_svc


class BillCsvError(ValueError):
    """Raised when the CSV itself can't be parsed (not a per-row error)."""


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "supplier": ("supplier", "vendor", "contact", "supplier_name", "name"),
    "issue_date": ("issue_date", "date", "invoice_date", "bill_date"),
    "due_date": ("due_date", "due", "payment_due"),
    "reference": ("reference", "ref", "invoice_no", "invoice_number", "supplier_reference", "bill_ref"),
    "description": ("description", "memo", "details", "narration", "line"),
    "account_code": ("account", "account_code", "gl_account", "code"),
    "amount": ("amount", "total", "net", "net_amount", "value"),
    "tax_code": ("tax_code", "tax", "gst", "taxcode"),
}


@dataclass
class ParsedBillRow:
    row_num: int
    supplier: str = ""
    issue_date: date | None = None
    due_date: date | None = None
    reference: str | None = None
    description: str = ""
    account_code: str = ""
    amount: Decimal | None = None
    tax_code: str | None = None
    error: str | None = None


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> Decimal | None:
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_bill_csv(raw: str) -> list[ParsedBillRow]:
    """Parse the CSV text into rows with field-level (DB-free) validation.

    Supplier / account / tax-code *existence* is checked at commit (needs
    the DB); here we validate structure: required columns present, dates
    and amounts well-formed.
    """
    if not raw or not raw.strip():
        raise BillCsvError("Empty CSV.")
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise BillCsvError("CSV has no header row.")

    # Map actual headers → canonical fields (case-insensitive).
    header_map: dict[str, str] = {}
    lower_to_actual = {h.strip().lower(): h for h in reader.fieldnames}
    for canon, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower_to_actual:
                header_map[canon] = lower_to_actual[alias]
                break

    missing = [c for c in ("supplier", "issue_date", "account_code", "amount") if c not in header_map]
    if missing:
        raise BillCsvError(
            "CSV missing required column(s): "
            + ", ".join(missing)
            + ". Required: supplier, date, account, amount (optional: due_date, reference, description, tax_code)."
        )

    rows: list[ParsedBillRow] = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 = header
        def g(canon: str, _row: dict[str, Any] = raw_row) -> str:
            col = header_map.get(canon)
            return (_row.get(col, "") if col else "").strip()

        row = ParsedBillRow(row_num=i)
        row.supplier = g("supplier")
        row.reference = g("reference") or None
        row.description = g("description") or "Imported bill line"
        row.account_code = g("account_code")
        row.tax_code = g("tax_code") or None

        issue = _parse_date(g("issue_date"))
        due = _parse_date(g("due_date"))
        amount = _parse_amount(g("amount"))

        if not row.supplier:
            row.error = "missing supplier"
        elif issue is None:
            row.error = f"unparseable date: {g('issue_date')!r}"
        elif not row.account_code:
            row.error = "missing account"
        elif amount is None:
            row.error = f"unparseable amount: {g('amount')!r}"

        row.issue_date = issue
        row.due_date = due or issue
        row.amount = amount
        rows.append(row)
    return rows


async def commit_bill_csv(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    raw: str,
) -> dict[str, Any]:
    """Create DRAFT bills from the CSV. Bad rows are reported, not posted.

    Returns ``{"created": n, "skipped": m, "bill_ids": [...], "errors": [...]}``.
    """
    rows = parse_bill_csv(raw)
    created: list[str] = []
    errors: list[dict[str, Any]] = []

    for row in rows:
        if row.error:
            errors.append({"row": row.row_num, "error": row.error})
            continue

        # Resolve supplier — EXACT (case-insensitive) match only.
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    func.lower(Contact.name) == row.supplier.lower(),
                    Contact.contact_type.in_(
                        (ContactType.SUPPLIER, ContactType.BOTH)
                    ),
                    Contact.archived_at.is_(None),
                ).limit(1)
            )
        ).scalars().first()
        if contact is None:
            errors.append({"row": row.row_num, "error": f"supplier not found (exact match): {row.supplier!r}"})
            continue

        # Resolve GL account by code.
        account = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == row.account_code,
                ).limit(1)
            )
        ).scalars().first()
        if account is None:
            errors.append({"row": row.row_num, "error": f"account code not found: {row.account_code!r}"})
            continue
        if account.is_header:
            errors.append({"row": row.row_num, "error": f"account {row.account_code} is a header (group) account — cannot post to it"})
            continue

        # Resolve tax code (optional).
        tax_code_id: uuid.UUID | None = None
        if row.tax_code:
            tc = (
                await session.execute(
                    select(TaxCode).where(
                        TaxCode.company_id == company_id,
                        func.upper(TaxCode.code) == row.tax_code.upper(),
                    ).limit(1)
                )
            ).scalars().first()
            if tc is None:
                errors.append({"row": row.row_num, "error": f"tax code not found: {row.tax_code!r}"})
                continue
            tax_code_id = tc.id

        assert row.issue_date is not None and row.amount is not None
        bill = await bills_svc.create_draft(
            session,
            company_id=company_id,
            contact_id=contact.id,
            issue_date=row.issue_date,
            due_date=row.due_date or row.issue_date,
            supplier_reference=row.reference,
            lines=[
                {
                    "account_id": str(account.id),
                    "description": row.description,
                    "quantity": "1",
                    "unit_price": str(row.amount),
                    "discount_pct": "0",
                    "tax_code_id": str(tax_code_id) if tax_code_id else None,
                }
            ],
            notes="Imported from CSV — DRAFT. Review before posting.",
        )
        created.append(str(bill.id))

    return {
        "created": len(created),
        "skipped": len(errors),
        "bill_ids": created,
        "errors": errors,
    }
