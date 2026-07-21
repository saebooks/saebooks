"""TPAR (Taxable Payments Annual Report) aggregator.

For a given financial year + company, walks paid bills and expenses to
contacts flagged ``is_tpar_supplier=true`` and produces one
``tpar_lines`` row per payee with the gross + GST totals for the FY.

Australian FY runs 1 July → 30 June. ATO TPAR is due 28 August
following the FY end.

A "payment" toward the TPAR total is a bill or expense that:
* is in POSTED status (not draft, not voided)
* has a non-null contact_id (the supplier — one-off vendors can be
  filtered separately if `tpar_one_offs=true` ever lands)
* payment_date (for expenses) or issue_date (for bills) falls in
  [fy_start, fy_end]
* the contact has ``is_tpar_supplier = true``

Caveat: this counts the **bill issue date** rather than the cash-paid
date — closer to the ATO accrual basis used by most builders.
A cash-basis variant can be added by joining payment_allocations.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from saebooks.jurisdictions.au.bde.tpar import BdePayee


@dataclass(frozen=True)
class TparPayee:
    """One TPAR payee row - gross/GST/net for a flagged supplier."""

    contact_id: uuid.UUID
    contact_name: str
    abn: str | None
    total_incl_gst: Decimal
    total_gst: Decimal
    total_excl_gst: Decimal


@dataclass(frozen=True)
class TparReport:
    """Read-only TPAR report for a period. Jinja attribute access in
    templates/reports/tpar.html resolves these fields directly."""

    from_date: date
    to_date: date
    payees: list[TparPayee]
    grand_total_incl_gst: Decimal
    grand_total_gst: Decimal
    grand_total_excl_gst: Decimal


async def tpar_report(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> TparReport:
    """Read-only Taxable Payments Annual Report for the period.

    Aggregates POSTED bills + expenses to TPAR-flagged suppliers
    (``contacts.is_tpar_supplier``) per payee. Pure SELECT — no run is
    persisted (unlike ``build_tpar_run``), so viewing the report has no
    side effects and never conflicts with a FINALISED/LODGED run. Tenant
    scoping is enforced by RLS (``app.current_tenant``) and, defensively,
    by ``company_id`` (a company belongs to exactly one tenant). Defaults
    to the current Australian financial year (1 Jul – 30 Jun) when dates
    are omitted. Shape matches templates/reports/tpar.html.
    """
    today = date.today()
    fy_year = today.year if today.month >= 7 else today.year - 1
    if from_date is None:
        from_date = date(fy_year, 7, 1)
    if to_date is None:
        to_date = date(fy_year + 1, 6, 30)

    rows = (
        await session.execute(
            text(
                """
                SELECT c.id, c.name, c.abn,
                       COALESCE(SUM(src.total), 0)     AS gross,
                       COALESCE(SUM(src.tax_total), 0) AS gst
                  FROM contacts c
                  JOIN (
                        SELECT contact_id, total, tax_total
                          FROM bills
                         WHERE company_id = :c AND status = 'POSTED'
                           AND archived_at IS NULL
                           AND issue_date BETWEEN :s AND :e
                        UNION ALL
                        SELECT contact_id, total, tax_total
                          FROM expenses
                         WHERE company_id = :c AND status = 'POSTED'
                           AND archived_at IS NULL
                           AND expense_date BETWEEN :s AND :e
                       ) src ON src.contact_id = c.id
                 WHERE c.company_id = :c
                   AND c.is_tpar_supplier = TRUE
                 GROUP BY c.id
                HAVING COALESCE(SUM(src.total), 0) > 0
                 ORDER BY gross DESC
                """
            ),
            {"c": str(company_id), "s": from_date, "e": to_date},
        )
    ).all()

    payees: list[TparPayee] = []
    grand_incl = Decimal("0")
    grand_gst = Decimal("0")
    for r in rows:
        gross = Decimal(str(r[3]))
        gst = Decimal(str(r[4]))
        payees.append(
            TparPayee(
                contact_id=uuid.UUID(str(r[0])),
                contact_name=r[1],
                abn=r[2],
                total_incl_gst=gross,
                total_gst=gst,
                total_excl_gst=gross - gst,
            )
        )
        grand_incl += gross
        grand_gst += gst

    return TparReport(
        from_date=from_date,
        to_date=to_date,
        payees=payees,
        grand_total_incl_gst=grand_incl,
        grand_total_gst=grand_gst,
        grand_total_excl_gst=grand_incl - grand_gst,
    )


async def build_tpar_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    fy_start: date,
    fy_end: date,
    notes: str | None = None,
) -> uuid.UUID:
    """Create (or replace DRAFT of) a TPAR run for the given FY.

    If a DRAFT run already exists for this company+fy_start, it is
    DELETEd first so the new aggregation is fresh. FINALISED / LODGED
    runs cannot be replaced — caller must void them first.

    Returns the new tpar_run id.
    """
    # Refuse to overwrite a finalised/lodged run.
    existing = (
        await session.execute(
            text(
                """
                SELECT id, status FROM tpar_runs
                WHERE company_id = :c AND fy_start = :s AND tenant_id = :t
                """
            ),
            {"c": str(company_id), "s": fy_start, "t": str(tenant_id)},
        )
    ).first()
    if existing is not None:
        if existing[1] not in ("DRAFT", "VOIDED"):
            raise ValueError(
                f"TPAR run for FY{fy_start.year} already in status "
                f"{existing[1]} — void it before regenerating"
            )
        # Drop the old DRAFT and its lines (CASCADE).
        await session.execute(
            text("DELETE FROM tpar_runs WHERE id = :id"),
            {"id": str(existing[0])},
        )

    # Create the new run with placeholder totals — populated below.
    new_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO tpar_runs
              (id, company_id, tenant_id, fy_start, fy_end, status, notes)
            VALUES
              (:id, :c, :t, :s, :e, 'DRAFT', :n)
            """
        ),
        {
            "id": str(new_id), "c": str(company_id), "t": str(tenant_id),
            "s": fy_start, "e": fy_end,
            "n": notes,
        },
    )

    # Aggregate bills + expenses per reportable contact via a single SQL
    # statement — avoids N+1 with thousands of rows.
    await session.execute(
        text(
            """
            INSERT INTO tpar_lines (
                id, tpar_run_id, contact_id, tenant_id,
                payee_name, payee_abn,
                payee_family_name, payee_given_name, payee_other_given_name,
                payee_phone, payee_email, payee_bsb, payee_account_number,
                payee_address_line1, payee_address_line2,
                payee_city, payee_state, payee_postcode, payee_country,
                gross_paid, gst_paid, bill_count, expense_count
            )
            SELECT
                gen_random_uuid(),
                :run_id,
                c.id,
                :t,
                c.name,
                c.abn,
                c.family_name,
                c.given_name,
                c.other_given_name,
                c.phone,
                c.email,
                c.bank_bsb,
                c.bank_account_number,
                c.address_line1,
                c.address_line2,
                c.city,
                c.state,
                c.postcode,
                c.country,
                COALESCE(SUM(src.total), 0)     AS gross_paid,
                COALESCE(SUM(src.tax_total), 0) AS gst_paid,
                COUNT(*) FILTER (WHERE src.kind = 'bill')    AS bill_count,
                COUNT(*) FILTER (WHERE src.kind = 'expense') AS expense_count
            FROM contacts c
            JOIN (
                SELECT contact_id, total, tax_total, issue_date AS doc_date, 'bill' AS kind
                  FROM bills
                 WHERE company_id = :c AND tenant_id = :t
                   AND status = 'POSTED'
                   AND archived_at IS NULL
                   AND issue_date BETWEEN :s AND :e
                UNION ALL
                SELECT contact_id, total, tax_total, expense_date, 'expense'
                  FROM expenses
                 WHERE company_id = :c AND tenant_id = :t
                   AND status = 'POSTED'
                   AND archived_at IS NULL
                   AND expense_date BETWEEN :s AND :e
            ) src ON src.contact_id = c.id
            WHERE c.company_id = :c
              AND c.tenant_id  = :t
              AND c.is_tpar_supplier = TRUE
            GROUP BY c.id
            HAVING COALESCE(SUM(src.total), 0) > 0
            """
        ),
        {
            "run_id": str(new_id), "c": str(company_id), "t": str(tenant_id),
            "s": fy_start, "e": fy_end,
        },
    )

    # Update the run totals from the inserted lines.
    await session.execute(
        text(
            """
            UPDATE tpar_runs r
               SET total_payee_count = sub.n,
                   total_gross_amount = sub.g,
                   total_gst_amount = sub.gst
              FROM (
                SELECT
                  COUNT(*) AS n,
                  COALESCE(SUM(gross_paid),0) AS g,
                  COALESCE(SUM(gst_paid),0) AS gst
                FROM tpar_lines WHERE tpar_run_id = :id
              ) sub
             WHERE r.id = :id
            """
        ),
        {"id": str(new_id)},
    )
    await session.commit()
    return new_id


async def finalise_tpar_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    finalised_by: str,
) -> None:
    """Move a DRAFT run to FINALISED. Locks subsequent edits."""
    result = await session.execute(
        text(
            """
            UPDATE tpar_runs
               SET status = 'FINALISED',
                   finalised_at = now(),
                   finalised_by = :by,
                   version = version + 1
             WHERE id = :id
               AND tenant_id = :t
               AND status = 'DRAFT'
            RETURNING id
            """
        ),
        {"id": str(run_id), "t": str(tenant_id), "by": finalised_by},
    )
    if result.first() is None:
        raise ValueError("TPAR run not found or not in DRAFT status")
    await session.commit()


async def get_tpar_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict | None:
    row = (
        await session.execute(
            text(
                """
                SELECT id, fy_start, fy_end, status, generated_at,
                       finalised_at, finalised_by, lodged_at, lodged_reference,
                       total_payee_count, total_gross_amount, total_gst_amount,
                       notes, version
                  FROM tpar_runs
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(run_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]), "fy_start": row[1].isoformat(), "fy_end": row[2].isoformat(),
        "status": row[3], "generated_at": row[4].isoformat() if row[4] else None,
        "finalised_at": row[5].isoformat() if row[5] else None,
        "finalised_by": row[6],
        "lodged_at": row[7].isoformat() if row[7] else None,
        "lodged_reference": row[8],
        "total_payee_count": row[9],
        "total_gross_amount": str(row[10]),
        "total_gst_amount": str(row[11]),
        "notes": row[12], "version": row[13],
    }


async def list_tpar_lines(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT contact_id, payee_name, payee_abn,
                       payee_address_line1, payee_address_line2,
                       payee_city, payee_state, payee_postcode, payee_country,
                       gross_paid, gst_paid, bill_count, expense_count,
                       tax_withheld, statement_by_supplier, amendment,
                       payee_family_name, payee_given_name, payee_other_given_name,
                       payee_phone, payee_email, payee_bsb, payee_account_number
                  FROM tpar_lines
                 WHERE tpar_run_id = :id AND tenant_id = :t
                 ORDER BY gross_paid DESC
                """
            ),
            {"id": str(run_id), "t": str(tenant_id)},
        )
    ).all()
    return [
        {
            "contact_id": str(r[0]), "payee_name": r[1], "payee_abn": r[2],
            "payee_address_line1": r[3], "payee_address_line2": r[4],
            "payee_city": r[5], "payee_state": r[6],
            "payee_postcode": r[7], "payee_country": r[8],
            "gross_paid": str(r[9]), "gst_paid": str(r[10]),
            "bill_count": r[11], "expense_count": r[12],
            "tax_withheld": str(r[13]),
            "statement_by_supplier": r[14], "amendment": r[15],
            "payee_family_name": r[16], "payee_given_name": r[17],
            "payee_other_given_name": r[18],
            "payee_phone": r[19], "payee_email": r[20],
            "payee_bsb": r[21], "payee_account_number": r[22],
        }
        for r in rows
    ]


async def build_tpar_bde_file_for_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    run_type: str = "P",
    software_developer: str = "SAE Books",
    sender_contact_name: str | None = None,
    sender_phone: str | None = None,
) -> bytes:
    """Render a TPAR run as the ATO BDE (FPAIVV03.0) flat file.

    The company is both payer and sender (self-lodger) — its name,
    ABN (via business_identifiers) and address JSONB feed both blocks.
    ``run_type="T"`` produces a file for the BDE test facility.
    Raises ``TparBdeError`` naming any field the file needs and the
    ledger doesn't have (incomplete payee address, missing name split
    for an individual, invalid state code, …).
    """
    from saebooks.jurisdictions.au.bde.tpar import (
        BdeAddress,
        BdePayer,
        BdeSender,
        TparBdeError,
        build_tpar_bde_file,
    )
    from saebooks.models.company import Company
    from saebooks.services.business_identifiers import primary_registry_identifier

    run = await get_tpar_run(
        session, tenant_id=tenant_id, company_id=company_id, run_id=run_id
    )
    if run is None:
        raise TparBdeError("TPAR run not found")
    lines = await list_tpar_lines(session, tenant_id=tenant_id, run_id=run_id)
    if not lines:
        raise TparBdeError("TPAR run has no payee lines")

    company = await session.get(Company, company_id)
    if company is None:
        raise TparBdeError("company not found")
    company_abn = await primary_registry_identifier(session, company)
    addr = company.address or {}
    # Company.address JSONB has no enforced key shape — live data carries
    # address_line1/city while the STP glue's companies use line1/suburb.
    company_address = BdeAddress(
        line1=addr.get("line1") or addr.get("address_line1") or "",
        line2=addr.get("line2") or addr.get("address_line2") or "",
        suburb=addr.get("suburb") or addr.get("city") or "",
        state=(addr.get("state") or "").upper(),
        postcode=addr.get("postcode") or "",
    )
    contact_name = sender_contact_name or company.legal_name or company.name
    phone = sender_phone or company.phone or ""

    fy_end = date.fromisoformat(run["fy_end"])
    payer = BdePayer(
        abn=company_abn,
        financial_year=fy_end.year,
        name=company.legal_name or company.name,
        trading_name=company.trading_name or "",
        address=company_address,
        contact_name=contact_name,
        phone=phone,
        email=company.email or "",
    )
    sender = BdeSender(
        abn=company_abn,
        name=company.legal_name or company.name,
        contact_name=contact_name,
        phone=phone,
        address=company_address,
        email=company.email or "",
        file_reference=f"TPAR{fy_end.year}",
    )

    payees = [_line_to_bde_payee(ln) for ln in lines]
    return build_tpar_bde_file(
        sender,
        payer,
        payees,
        software_developer=software_developer,
        run_type=run_type,
        report_end_date=fy_end,
    )


def _line_to_bde_payee(line: dict) -> BdePayee:
    """Map one ``tpar_lines`` row (as dicted by ``list_tpar_lines``) onto
    the flat file's DPAIVS shape.

    A line with a family name is an individual (business name blank,
    spec 6.48); otherwise the display name is the business name (6.51).
    A non-Australian country flips the address to the overseas shape —
    state OTH + postcode 9999 + country named (6.55-6.57).
    """
    from saebooks.jurisdictions.au.bde.tpar import BdeAddress, BdePayee

    country = (line.get("payee_country") or "").strip()
    domestic = country.upper() in ("", "AU", "AUS", "AUSTRALIA")
    address = BdeAddress(
        line1=line.get("payee_address_line1") or "",
        line2=line.get("payee_address_line2") or "",
        suburb=line.get("payee_city") or "",
        state=(line.get("payee_state") or "").upper() if domestic else "OTH",
        postcode=(line.get("payee_postcode") or "") if domestic else "9999",
        country="" if domestic else country,
    )
    family = (line.get("payee_family_name") or "").strip()
    return BdePayee(
        address=address,
        gross=line["gross_paid"],
        tax_withheld=line.get("tax_withheld") or 0,
        gst=line["gst_paid"],
        abn=line.get("payee_abn") or "",
        business_name="" if family else (line.get("payee_name") or ""),
        family_name=family,
        given_name=(line.get("payee_given_name") or "") if family else "",
        other_given_name=(line.get("payee_other_given_name") or "") if family else "",
        phone=line.get("payee_phone") or "",
        bsb=line.get("payee_bsb") or "",
        account_number=line.get("payee_account_number") or "",
        email=line.get("payee_email") or "",
        statement_by_supplier=bool(line.get("statement_by_supplier")),
        amendment=bool(line.get("amendment")),
    )


def lines_to_csv(lines: list[dict]) -> bytes:
    """Render TPAR lines to a basic CSV. NOT the ATO-spec TPAR file — that
    requires a specific .tpar format produced by ATO portal-compatible
    software. This is the bookkeeper-friendly export for reconciliation
    before manual data entry into the ATO portal.
    """
    import csv
    from io import StringIO
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Payee name", "ABN",
        "Address line 1", "Address line 2", "City", "State", "Postcode",
        "Gross paid (inc GST)", "GST paid",
        "Bill count", "Expense count",
    ])
    for ln in lines:
        w.writerow([
            ln["payee_name"], ln["payee_abn"] or "",
            ln["payee_address_line1"] or "",
            ln["payee_address_line2"] or "",
            ln["payee_city"] or "",
            ln["payee_state"] or "",
            ln["payee_postcode"] or "",
            ln["gross_paid"],
            ln["gst_paid"],
            ln["bill_count"],
            ln["expense_count"],
        ])
    return buf.getvalue().encode("utf-8")
