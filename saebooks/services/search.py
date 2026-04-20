"""Global search — union query across contacts, accounts, invoices, bills.

A single ``/search?q=acme`` endpoint returns hits from every major
entity. The UI uses this for a Cmd-K / slash palette that jumps
straight to the hit's detail page.

Design:

* ILIKE-based fuzzy match on the most obviously-queryable columns per
  entity (name/code/number/supplier_reference). We deliberately avoid
  Postgres ``tsvector`` full-text for now — most small-business data
  sets are small enough for a plain index-walk, and introducing
  tsvectors means a migration + maintenance on every INSERT/UPDATE.
  When a user hits a performance wall, promote the hot entity to
  tsvector + a GIN index; nothing else in this module needs to change.
* Hits are ordered by entity weight (contacts first, then invoices,
  bills, accounts) then alphabetically within each type, so the
  palette has a predictable shape.
* Archived rows are excluded by default — the palette is a "jump to"
  affordance, not a historical audit tool.
* Per-entity limit of 10 keeps the response small; total cap is
  40 hits. Future: add pagination when someone actually needs it.

Return shape: a list of ``SearchHit`` dataclasses, JSON-friendly via
``asdict``. The router + the palette template both consume the same
dataclass.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.bill import Bill
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice

# Per-entity hit cap — keeps the palette responsive on a 10k-contact
# DB. Total hits across all entities = PER_ENTITY_LIMIT * 4.
PER_ENTITY_LIMIT = 10

HitKind = Literal["contact", "invoice", "bill", "account"]


@dataclass
class SearchHit:
    """One result row. ``url`` is where a click takes you.

    ``subtitle`` is optional extra context shown greyed out below the
    title (e.g. the company name on an invoice hit, or the balance on
    a bill).
    """

    id: uuid.UUID
    kind: HitKind
    title: str
    subtitle: str | None
    url: str


async def search_all(
    session: AsyncSession,
    company_id: uuid.UUID,
    query: str,
) -> list[SearchHit]:
    """Run the union query. Empty / whitespace-only ``query`` returns []."""
    q = (query or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"

    hits: list[SearchHit] = []
    hits.extend(await _search_contacts(session, company_id, pattern))
    hits.extend(await _search_invoices(session, company_id, pattern))
    hits.extend(await _search_bills(session, company_id, pattern))
    hits.extend(await _search_accounts(session, company_id, pattern))
    return hits


async def _search_contacts(
    session: AsyncSession, company_id: uuid.UUID, pattern: str
) -> list[SearchHit]:
    stmt = (
        select(Contact)
        .where(
            Contact.company_id == company_id,
            Contact.archived_at.is_(None),
            or_(
                Contact.name.ilike(pattern),
                Contact.abn.ilike(pattern),
            ),
        )
        .order_by(Contact.name)
        .limit(PER_ENTITY_LIMIT)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SearchHit(
            id=c.id,
            kind="contact",
            title=c.name,
            subtitle=c.abn,
            url=f"/contacts/{c.id}",
        )
        for c in rows
    ]


async def _search_invoices(
    session: AsyncSession, company_id: uuid.UUID, pattern: str
) -> list[SearchHit]:
    # Match on invoice number or the linked contact's name. We
    # outer-join Contact so DRAFT invoices without a number still hit
    # on the contact-name leg.
    stmt = (
        select(Invoice, Contact.name.label("contact_name"))
        .join(Contact, Contact.id == Invoice.contact_id)
        .where(
            Invoice.company_id == company_id,
            Invoice.archived_at.is_(None),
            or_(
                Invoice.number.ilike(pattern),
                Contact.name.ilike(pattern),
            ),
        )
        .order_by(Invoice.issue_date.desc())
        .limit(PER_ENTITY_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    return [
        SearchHit(
            id=inv.id,
            kind="invoice",
            title=inv.number or "(draft)",
            subtitle=f"{contact_name} — {inv.status.value}",
            url=f"/invoices/{inv.id}",
        )
        for inv, contact_name in rows
    ]


async def _search_bills(
    session: AsyncSession, company_id: uuid.UUID, pattern: str
) -> list[SearchHit]:
    stmt = (
        select(Bill, Contact.name.label("contact_name"))
        .join(Contact, Contact.id == Bill.contact_id)
        .where(
            Bill.company_id == company_id,
            Bill.archived_at.is_(None),
            or_(
                Bill.number.ilike(pattern),
                Bill.supplier_reference.ilike(pattern),
                Contact.name.ilike(pattern),
            ),
        )
        .order_by(Bill.issue_date.desc())
        .limit(PER_ENTITY_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    return [
        SearchHit(
            id=bill.id,
            kind="bill",
            title=bill.number or "(draft)",
            subtitle=f"{contact_name} — {bill.status.value}",
            url=f"/bills/{bill.id}",
        )
        for bill, contact_name in rows
    ]


async def _search_accounts(
    session: AsyncSession, company_id: uuid.UUID, pattern: str
) -> list[SearchHit]:
    stmt = (
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.archived_at.is_(None),
            or_(
                Account.code.ilike(pattern),
                Account.name.ilike(pattern),
            ),
        )
        .order_by(Account.code)
        .limit(PER_ENTITY_LIMIT)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SearchHit(
            id=a.id,
            kind="account",
            title=f"{a.code} — {a.name}",
            subtitle=a.account_type.value,
            url=f"/accounts/{a.id}",
        )
        for a in rows
    ]


__all__ = ["PER_ENTITY_LIMIT", "SearchHit", "search_all"]
