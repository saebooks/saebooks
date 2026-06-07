"""Paperless-ngx → SAE Books inbound ingest.

Turns a Paperless ``document-added`` webhook into a **DRAFT** supplier
bill for human review. Design rule (Richard, 2026-06-04): *Paperless must
never be able to corrupt the books.* Therefore this code:

* creates the bill as ``DRAFT`` and **never posts** it — nothing touches
  the general ledger until a human reviews and posts it in the GUI;
* creates it with **no GL lines** (total 0) — the reviewer enters the
  real lines/accounts from the attached document, so the ingest never
  guesses an expense account or amount into a postable shape;
* is **idempotent** on the Paperless document id (``supplier_reference =
  "PL-<id>"``) — a re-fired webhook updates nothing and creates no
  duplicate;
* is **fail-safe** — if Paperless is unreachable or extraction fails it
  still creates the draft shell (with the failure noted) and never
  raises out of the webhook;
* resolves the supplier only on an **exact** name match, else parks the
  draft on a per-tenant placeholder contact — it never silently attaches
  a bill to the wrong supplier.

The extracted figures are written into the bill's ``notes`` for the
reviewer to copy from — they are explicitly NOT turned into GL lines.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.bill import Bill
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.services import bills as bills_svc
from saebooks.services import contacts as contacts_svc
from saebooks.services.ai_extraction import extract_document
from saebooks.services.integrations.paperless import (
    PaperlessClient,
    PaperlessError,
)

logger = logging.getLogger("saebooks.paperless_ingest")

_PLACEHOLDER_NAME = "Paperless Intake (unresolved)"
_REVIEW_BANNER = (
    "AUTO-INGESTED FROM PAPERLESS — DRAFT ONLY. Nothing has hit the ledger. "
    "Review against the attached document, set the supplier, and enter the GL "
    "lines/accounts before posting."
)


def extract_document_id(payload: dict[str, Any]) -> int | None:
    """Best-effort pull of the Paperless document id from a webhook body.

    Paperless workflow webhooks vary in shape; accept the common keys.
    """
    for key in ("id", "document_id", "pk", "doc_pk", "documentId"):
        val = payload.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    doc = payload.get("document")
    if isinstance(doc, dict):
        nested = extract_document_id(doc)
        if nested is not None:
            return nested
    # Paperless-ngx workflow webhooks expose no raw-pk placeholder; only the
    # ``doc_url`` value (".../documents/<pk>/") carries the id. Pull it out so
    # the operator can send ``{"doc_url": "{{ doc_url }}"}`` as the body.
    for url_key in ("doc_url", "document_url", "url"):
        url_val = payload.get(url_key)
        if isinstance(url_val, str):
            m = re.search(r"/documents/(\d+)", url_val)
            if m:
                return int(m.group(1))
    return None


def _parse_date(raw: object) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


async def _placeholder_contact(
    session: AsyncSession, company_id: uuid.UUID, tenant_id: uuid.UUID
) -> Contact:
    """Find or create the per-tenant 'unresolved supplier' placeholder."""
    existing = (
        await session.execute(
            select(Contact).where(
                Contact.company_id == company_id,
                Contact.name == _PLACEHOLDER_NAME,
            ).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing
    return await contacts_svc.create(
        session,
        company_id,
        actor="paperless-webhook",
        tenant_id=tenant_id,
        name=_PLACEHOLDER_NAME,
        contact_type=ContactType.SUPPLIER,
        notes="Auto-created placeholder for Paperless ingests whose supplier "
        "could not be matched. Reassign each draft bill to the real supplier.",
    )


def _build_notes(
    *,
    document_id: int,
    title: str,
    browser_url: str | None,
    extracted: dict[str, Any],
    extract_error: str | None,
    placeholder_used: bool,
) -> str:
    lines: list[str] = [f"⚠ {_REVIEW_BANNER}", ""]
    lines.append(f"Paperless document #{document_id}: {title}")
    if browser_url:
        lines.append(f"Open original: {browser_url}")
    lines.append("")
    if placeholder_used:
        lines.append(
            "⚠ Supplier NOT matched — parked on the placeholder contact. "
            "Set the real supplier before posting."
        )
    lines.append("--- Extracted (for reference only — NOT posted) ---")
    lines.append(f"Vendor: {extracted.get('vendor_name') or '(none)'}")
    lines.append(f"Date: {extracted.get('date') or '(none)'}")
    lines.append(f"Due: {extracted.get('due_date') or '(none)'}")
    lines.append(f"Subtotal: {extracted.get('subtotal') or '(none)'}")
    lines.append(f"Tax: {extracted.get('tax_amount') or '(none)'}")
    lines.append(f"Total: {extracted.get('total') or '(none)'}")
    items = extracted.get("line_items") or []
    if isinstance(items, list) and items:
        lines.append("Line items:")
        for it in items:
            if not isinstance(it, dict):
                continue
            lines.append(
                f"  - {it.get('description') or '?'} | qty {it.get('qty') or ''} "
                f"@ {it.get('unit_price') or ''} = {it.get('amount') or ''} "
                f"[{it.get('tax_code') or ''}]"
            )
    if extract_error:
        lines.append("")
        lines.append(f"⚠ Extraction incomplete: {extract_error}")
    return "\n".join(lines)


async def ingest_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: int,
    settings: Settings,
) -> dict[str, Any]:
    """Create (or skip) a DRAFT bill for a Paperless document.

    ``session`` MUST already be tenant-bound (``session.info['tenant_id']``
    set so the after_begin listener applies ``app.current_tenant``).
    Returns a small status dict; never raises for expected failure modes.
    """
    ref = f"PL-{document_id}"

    # --- Idempotency: one draft per Paperless document, ever. ---
    dupe = (
        await session.execute(
            select(Bill).where(Bill.supplier_reference == ref).limit(1)
        )
    ).scalars().first()
    if dupe is not None:
        logger.info("paperless ingest: doc %s already ingested as bill %s", document_id, dupe.id)
        return {"status": "duplicate", "bill_id": str(dupe.id)}

    # --- Resolve the company within the tenant (single-company tenants). ---
    company = (
        await session.execute(
            select(Company)
            .where(Company.archived_at.is_(None))
            .order_by(Company.created_at)
            .limit(1)
        )
    ).scalars().first()
    if company is None:
        logger.warning("paperless ingest: no active company for tenant %s", tenant_id)
        return {"status": "no_company"}
    session.info["company_id"] = company.id

    # --- Fetch + extract (best-effort; failure still yields a draft shell). ---
    title = f"Document {document_id}"
    browser_url: str | None = None
    extracted: dict[str, Any] = {}
    extract_error: str | None = None
    try:
        async with PaperlessClient(settings=settings) as pc:
            attach = await pc.fetch_attachment(document_id)
            title = attach.title
            browser_url = attach.browser_url
            content, mime = await pc.download_content(document_id)
        extracted = await extract_document(content, mime or attach.mime_type or "application/pdf")
        extract_error = extracted.get("extraction_error")
    except (PaperlessError, Exception) as exc:
        extract_error = f"fetch/extract failed: {exc}"
        logger.warning("paperless ingest: %s (doc %s)", extract_error, document_id)

    # --- Resolve supplier: exact match only, else placeholder. ---
    vendor = str(extracted.get("vendor_name") or "").strip()
    contact: Contact | None = None
    if vendor:
        try:
            matches = await contacts_svc.search_by_name(
                session, company.id, vendor, tenant_id=tenant_id
            )
            exact = [
                c
                for c in matches
                if (c.name or "").strip().lower() == vendor.lower()
                and c.contact_type
                in (ContactType.SUPPLIER, ContactType.CONTRACTOR, ContactType.BOTH)
            ]
            if len(exact) == 1:
                contact = exact[0]
        except Exception as exc:
            logger.warning("paperless ingest: contact match failed: %s", exc)
    placeholder_used = contact is None
    if contact is None:
        contact = await _placeholder_contact(session, company.id, tenant_id)

    issue = _parse_date(extracted.get("date")) or date.today()
    due = _parse_date(extracted.get("due_date")) or issue

    notes = _build_notes(
        document_id=document_id,
        title=title,
        browser_url=browser_url,
        extracted=extracted,
        extract_error=extract_error,
        placeholder_used=placeholder_used,
    )

    # --- Create the DRAFT bill: no GL lines, never posted. ---
    bill = await bills_svc.create_draft(
        session,
        company_id=company.id,
        contact_id=contact.id,
        issue_date=issue,
        due_date=due,
        supplier_reference=ref,
        lines=None,
        notes=notes,
    )
    logger.info(
        "paperless ingest: created DRAFT bill %s for doc %s (tenant %s, placeholder=%s)",
        bill.id, document_id, tenant_id, placeholder_used,
    )
    return {
        "status": "created",
        "bill_id": str(bill.id),
        "placeholder_supplier": placeholder_used,
        "extraction": "partial" if extract_error else "ok",
    }
