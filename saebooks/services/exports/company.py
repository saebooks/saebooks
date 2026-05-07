"""Per-company data export — everything we hold for one company, as a ZIP.

Generalises ``services/bank_feeds/export.py`` (which dumps just the
bank-feed artefacts as a CDR compliance bundle) to cover the whole
ledger: company record, contacts, chart of accounts, journal, invoices,
bills, credit notes, payments, fixed assets, bank feeds, and the audit
trail.

Output shape inside the zip::

    company-<id>-<ts>/
        company.json
        contacts.json
        accounts.json
        tax_codes.json
        journal_entries.json          (includes lines)
        invoices.json                 (includes lines)
        bills.json                    (includes lines)
        credit_notes.json             (includes lines)
        payments.json                 (includes allocations)
        fixed_assets.json
        bank_feed_accounts.json
        bank_statement_lines.json
        audit.csv                     (the 5-year trail)
        README.txt                    (what's in here + who exported it)

Everything is JSON + CSV — no binary attachments in v1. Paperless-linked
documents stay in Paperless; the export references them by URL inside
``journal_entries.attachments`` so a consumer can go fetch the PDF with
their own Paperless token.

The service is GDPR / CDR spirit — a customer can demand "everything
you have on me" and this is what they get. Callers (admin UI,
offboarding flow) decide encryption / delivery.
"""
from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import selectinload

from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.credit_note import CreditNote
from saebooks.models.fixed_asset import FixedAsset
from saebooks.models.invoice import Invoice
from saebooks.models.journal import JournalEntry
from saebooks.models.payment import Payment
from saebooks.models.tax_code import TaxCode
from saebooks.services import audit as audit_svc


def _row_to_dict(obj: Any) -> dict[str, Any]:
    """JSON-safe dict from any ORM row — UUIDs/datetimes/enums/Decimals as strings."""
    mapper = inspect(type(obj))
    data: dict[str, Any] = {}
    for col in mapper.columns:
        val = getattr(obj, col.key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime) or hasattr(val, "isoformat"):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # enums
            val = val.value
        data[col.key] = val
    return data


def _serialise(rows: list[Any]) -> list[dict[str, Any]]:
    return [_row_to_dict(r) for r in rows]


def _serialise_with_lines(
    parents: list[Any], lines_attr: str
) -> list[dict[str, Any]]:
    """Dump each parent row + its ``lines_attr`` collection."""
    out = []
    for p in parents:
        d = _row_to_dict(p)
        lines = getattr(p, lines_attr, None) or []
        d[lines_attr] = [_row_to_dict(ln) for ln in lines]
        out.append(d)
    return out


async def build_company_export(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    exported_by: str | None = None,
    include_audit: bool = True,
) -> tuple[bytes, str]:
    """Build the zip bundle in memory. Returns ``(zip_bytes, filename)``.

    Callers (router) then stream the bytes to the client with a
    ``Content-Disposition: attachment; filename=<filename>`` header.
    Keeping this in memory is fine — even a busy SMB ledger is
    megabytes, not gigabytes, and streaming-zip is a later
    optimisation.
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company {company_id} not found")

    # --- pull every scope ---------------------------------------------
    contacts = (
        await session.execute(
            select(Contact)
            .where(Contact.company_id == company_id)
            .order_by(Contact.name)
        )
    ).scalars().all()

    accounts = (
        await session.execute(
            select(Account)
            .where(Account.company_id == company_id)
            .order_by(Account.code)
        )
    ).scalars().all()

    tax_codes = (
        await session.execute(
            select(TaxCode)
            .where(TaxCode.company_id == company_id)
            .order_by(TaxCode.code)
        )
    ).scalars().all()

    journal_entries = (
        await session.execute(
            select(JournalEntry)
            .where(JournalEntry.company_id == company_id)
            .options(selectinload(JournalEntry.lines))
            .order_by(JournalEntry.entry_date, JournalEntry.ref)
        )
    ).scalars().all()

    invoices = (
        await session.execute(
            select(Invoice)
            .where(Invoice.company_id == company_id)
            .options(selectinload(Invoice.lines))
            .order_by(Invoice.issue_date)
        )
    ).scalars().all()

    bills = (
        await session.execute(
            select(Bill)
            .where(Bill.company_id == company_id)
            .options(selectinload(Bill.lines))
            .order_by(Bill.issue_date)
        )
    ).scalars().all()

    credit_notes = (
        await session.execute(
            select(CreditNote)
            .where(CreditNote.company_id == company_id)
            .options(selectinload(CreditNote.lines))
            .order_by(CreditNote.issue_date)
        )
    ).scalars().all()

    payments = (
        await session.execute(
            select(Payment)
            .where(Payment.company_id == company_id)
            .options(selectinload(Payment.allocations))
            .order_by(Payment.payment_date)
        )
    ).scalars().all()

    fixed_assets = (
        await session.execute(
            select(FixedAsset)
            .where(FixedAsset.company_id == company_id)
            .order_by(FixedAsset.code)
        )
    ).scalars().all()

    feed_accounts = (
        await session.execute(
            select(BankFeedAccount)
            .where(BankFeedAccount.company_id == company_id)
        )
    ).scalars().all()
    feed_account_ids = [f.id for f in feed_accounts]

    statement_lines: list[BankStatementLine] = []
    if feed_account_ids:
        statement_lines = list(
            (
                await session.execute(
                    select(BankStatementLine).where(
                        BankStatementLine.bank_feed_account_id.in_(feed_account_ids)
                    )
                    .order_by(BankStatementLine.txn_date)
                )
            ).scalars().all()
        )

    # Audit trail — filter by table_name-scoped rows? No: audit rows
    # don't carry company_id. We just dump everything, which is fine
    # for single-tenant Community + not ideal for multi-tenant
    # Enterprise (upgrade later with a per-company scoper column).
    audit_csv = ""
    if include_audit:
        audit_csv = await audit_svc.export_csv(session)

    # --- build the zip in memory --------------------------------------
    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    root = f"company-{company_id}-{ts}"
    filename = f"{root}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_json(zf, f"{root}/company.json", _row_to_dict(company))
        _write_json(zf, f"{root}/contacts.json", _serialise(list(contacts)))
        _write_json(zf, f"{root}/accounts.json", _serialise(list(accounts)))
        _write_json(zf, f"{root}/tax_codes.json", _serialise(list(tax_codes)))
        _write_json(
            zf,
            f"{root}/journal_entries.json",
            _serialise_with_lines(list(journal_entries), "lines"),
        )
        _write_json(
            zf,
            f"{root}/invoices.json",
            _serialise_with_lines(list(invoices), "lines"),
        )
        _write_json(
            zf,
            f"{root}/bills.json",
            _serialise_with_lines(list(bills), "lines"),
        )
        _write_json(
            zf,
            f"{root}/credit_notes.json",
            _serialise_with_lines(list(credit_notes), "lines"),
        )
        _write_json(
            zf,
            f"{root}/payments.json",
            _serialise_with_lines(list(payments), "allocations"),
        )
        _write_json(zf, f"{root}/fixed_assets.json", _serialise(list(fixed_assets)))
        _write_json(
            zf,
            f"{root}/bank_feed_accounts.json",
            _serialise(list(feed_accounts)),
        )
        _write_json(
            zf,
            f"{root}/bank_statement_lines.json",
            _serialise(list(statement_lines)),
        )
        if include_audit:
            zf.writestr(f"{root}/audit.csv", audit_csv)

        readme = _build_readme(
            company=company,
            exported_at=now,
            exported_by=exported_by,
            counts={
                "contacts": len(contacts),
                "accounts": len(accounts),
                "tax_codes": len(tax_codes),
                "journal_entries": len(journal_entries),
                "invoices": len(invoices),
                "bills": len(bills),
                "credit_notes": len(credit_notes),
                "payments": len(payments),
                "fixed_assets": len(fixed_assets),
                "bank_feed_accounts": len(feed_accounts),
                "bank_statement_lines": len(statement_lines),
            },
        )
        zf.writestr(f"{root}/README.txt", readme)

    return buf.getvalue(), filename


def _write_json(
    zf: zipfile.ZipFile, name: str, payload: dict[str, Any] | list[Any]
) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    zf.writestr(name, body)


def _build_readme(
    *,
    company: Company,
    exported_at: datetime,
    exported_by: str | None,
    counts: dict[str, int],
) -> str:
    lines = [
        "SAE Books — Company data export",
        "=" * 60,
        "",
        f"Company:          {company.name}",
        f"Company ID:       {company.id}",
        f"Exported at:      {exported_at.isoformat()}",
        f"Exported by:      {exported_by or 'unknown'}",
        "",
        "Row counts:",
    ]
    for k, v in counts.items():
        lines.append(f"  {k:<26} {v}")
    lines.extend(
        [
            "",
            "File layout:",
            "  company.json              The company row itself",
            "  contacts.json             All customers + suppliers",
            "  accounts.json             Chart of accounts",
            "  tax_codes.json            Tax codes (GST rules)",
            "  journal_entries.json      Every GL entry + its lines",
            "  invoices.json             AR — invoices + lines",
            "  bills.json                AP — bills + lines",
            "  credit_notes.json         AR credit memos + lines",
            "  payments.json             Payments + allocations",
            "  fixed_assets.json         Fixed-asset register",
            "  bank_feed_accounts.json   Linked bank-feed accounts",
            "  bank_statement_lines.json Imported bank lines",
            "  audit.csv                 5-year audit trail (all tables)",
            "",
            "All money is stored as decimal-formatted strings to preserve",
            "precision across languages/tools. Dates/datetimes are ISO-8601.",
            "Paperless-linked documents remain in Paperless; this export",
            "references them by URL inside journal_entries.attachments.",
        ]
    )
    return "\n".join(lines) + "\n"


async def write_company_export(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    export_dir: str,
    exported_by: str | None = None,
    include_audit: bool = True,
) -> str:
    """Write the export to ``export_dir``; returns the resolved path.

    Convenience wrapper around :func:`build_company_export` for cron /
    offboarding jobs that prefer files on disk.
    """
    os.makedirs(export_dir, exist_ok=True)
    payload, filename = await build_company_export(
        session,
        company_id=company_id,
        exported_by=exported_by,
        include_audit=include_audit,
    )
    path = os.path.join(export_dir, filename)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path
