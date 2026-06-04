"""ingest.py — end-to-end supplier statement ingestion pipeline.

Orchestrates:
    1. Fetch OCR text via PaperlessClient.get_document() (the ``content`` field)
    2. Extract structured statement via extract_statement()
    3. Persist SupplierStatement + SupplierStatementLine rows (tenant-scoped)
    4. Resolve supplier contact (exact case-insensitive name match)
    5. Load supplier's active bills
    6. reconcile_lines() — sets match_status + notes
    7. Apply gate logic to set status:
        - AP/AR + readability gate: supplier_name is None OR closing_balance < 0
          → DISMISSED
        - Balance gate: |sum(open invoice lines) - closing_balance| > 0.01
          → NEEDS_REVIEW + escalation attempt with opus
        - Open exceptions (MISSING_IN_BOOKS / AMOUNT_MISMATCH): EXTRACTED
        - Fully clean: RECONCILED
    8. Write back our_ap_as_at, balance_delta, status, extraction_meta
    9. Idempotency: if SupplierStatement already exists for (tenant, source_document_id),
       update it in place.

Never posts to the GL.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact, ContactType
from saebooks.models.supplier_statement import (
    StatementLineType,
    StatementMatchStatus,
    StatementStatus,
    SupplierStatement,
    SupplierStatementLine,
)
from saebooks.services.integrations.paperless import PaperlessClient
from saebooks.services.statements.extract import ExtractedStatement, extract_statement
from saebooks.services.statements.reconcile import reconcile_lines

logger = logging.getLogger("saebooks.statements.ingest")

_CENT = Decimal("0.01")


async def ingest_statement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    paperless_document_id: int,
    settings: Settings,
) -> SupplierStatement:
    """Ingest a Paperless document as a supplier statement.

    Idempotent: re-ingesting the same (tenant, source_document_id) updates
    the existing row rather than creating a duplicate.
    """
    # Set tenant/company context for RLS
    session.info["tenant_id"] = tenant_id
    session.info["company_id"] = company_id

    # ------------------------------------------------------------------
    # 1. Idempotency: look for an existing statement for this document
    # ------------------------------------------------------------------
    existing_stmt = await _find_existing(session, tenant_id, paperless_document_id)

    # ------------------------------------------------------------------
    # 2. Fetch OCR text from Paperless
    # ------------------------------------------------------------------
    async with PaperlessClient(settings=settings) as pc:
        doc_meta = await pc.get_document(paperless_document_id)
    ocr_text: str = doc_meta.get("content") or ""

    # ------------------------------------------------------------------
    # 3. Extract with primary model
    # ------------------------------------------------------------------
    extracted = await extract_statement(ocr_text, settings=settings)
    extraction_meta: dict = {
        "model_used": extracted.model_used,
        "escalated": False,
        "escalation_resolved": None,
    }

    # ------------------------------------------------------------------
    # 4. AP/AR + readability gate (before persisting lines)
    # ------------------------------------------------------------------
    if extracted.supplier_name is None or (
        extracted.closing_balance is not None and extracted.closing_balance < 0
    ):
        stmt = await _upsert_header(
            session,
            existing=existing_stmt,
            tenant_id=tenant_id,
            company_id=company_id,
            extracted=extracted,
            paperless_document_id=paperless_document_id,
            status=StatementStatus.DISMISSED.value,
            extraction_meta={
                **extraction_meta,
                "dismissed_reason": "not an AP supplier statement",
            },
            our_ap_as_at=None,
            balance_delta=None,
        )
        await session.flush()
        await _upsert_lines(session, stmt, extracted)
        return stmt

    # ------------------------------------------------------------------
    # 5. Balance gate — check if open invoice lines reconcile to closing_balance
    # ------------------------------------------------------------------
    open_invoice_sum = sum(
        line.amount
        for line in extracted.lines
        if line.line_type == "invoice"
    )
    closing = extracted.closing_balance or Decimal("0")
    balance_discrepancy = abs(open_invoice_sum - closing)

    escalated_extraction: ExtractedStatement | None = None
    if balance_discrepancy > _CENT:
        logger.info(
            "balance gate tripped (discrepancy=%s); escalating to %s",
            balance_discrepancy,
            settings.statement_llm_model_escalation,
        )
        try:
            escalated_extraction = await extract_statement(
                ocr_text,
                settings=settings,
                model_override=settings.statement_llm_model_escalation,
            )
            # Check if escalated parse resolves the discrepancy
            esc_open_sum = sum(
                line.amount
                for line in escalated_extraction.lines
                if line.line_type == "invoice"
            )
            esc_closing = escalated_extraction.closing_balance or Decimal("0")
            esc_discrepancy = abs(esc_open_sum - esc_closing)

            if esc_discrepancy <= _CENT:
                # Escalation fixed it — use the escalated extraction
                extracted = escalated_extraction
                extraction_meta["escalated"] = True
                extraction_meta["model_used"] = escalated_extraction.model_used
                extraction_meta["escalation_resolved"] = True
                balance_discrepancy = esc_discrepancy
            else:
                extraction_meta["escalated"] = True
                extraction_meta["model_used_primary"] = extraction_meta["model_used"]
                extraction_meta["model_used"] = escalated_extraction.model_used
                extraction_meta["escalation_resolved"] = False
                extraction_meta["balance_discrepancy"] = str(balance_discrepancy)
        except Exception as exc:
            logger.warning("escalation attempt failed: %s", exc)
            extraction_meta["escalated"] = True
            extraction_meta["escalation_error"] = str(exc)
            extraction_meta["escalation_resolved"] = False
            extraction_meta["balance_discrepancy"] = str(balance_discrepancy)

    # ------------------------------------------------------------------
    # 6. Persist header + lines
    # ------------------------------------------------------------------
    stmt = await _upsert_header(
        session,
        existing=existing_stmt,
        tenant_id=tenant_id,
        company_id=company_id,
        extracted=extracted,
        paperless_document_id=paperless_document_id,
        status=StatementStatus.PENDING_EXTRACT.value,  # will be updated after recon
        extraction_meta=extraction_meta,
        our_ap_as_at=None,
        balance_delta=None,
    )
    await session.flush()
    fresh_lines = await _upsert_lines(session, stmt, extracted)

    # ------------------------------------------------------------------
    # 7. Resolve supplier contact
    # ------------------------------------------------------------------
    if extracted.supplier_name:
        contact = await _resolve_contact(session, company_id, extracted.supplier_name)
        if contact is not None:
            stmt.contact_id = contact.id

    # ------------------------------------------------------------------
    # 8. Load supplier's bills and reconcile
    # ------------------------------------------------------------------
    bills = await _load_supplier_bills(session, company_id, stmt.contact_id)
    # Pass lines explicitly to avoid triggering lazy-load on stmt.lines
    summary = reconcile_lines(stmt, bills, statement_lines=fresh_lines)

    # Persist the NOT_ON_STATEMENT synthetic lines returned by reconcile
    for synthetic in summary.not_on_statement_lines:
        session.add(synthetic)

    # ------------------------------------------------------------------
    # 9. Apply final status gates
    # ------------------------------------------------------------------
    if balance_discrepancy > _CENT:
        # Balance gate still tripped after escalation attempt
        final_status = StatementStatus.NEEDS_REVIEW.value
    elif summary.open_exceptions:
        # Open exceptions but balance ties
        final_status = StatementStatus.EXTRACTED.value
    else:
        final_status = StatementStatus.RECONCILED.value

    stmt.status = final_status
    stmt.our_ap_as_at = summary.our_ap_as_at
    stmt.balance_delta = summary.balance_delta
    stmt.extraction_meta = {
        **extraction_meta,
        "recon_counts": summary.counts,
    }

    return stmt


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _find_existing(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_document_id: int,
) -> SupplierStatement | None:
    result = await session.execute(
        select(SupplierStatement).where(
            SupplierStatement.tenant_id == tenant_id,
            SupplierStatement.source_document_id == source_document_id,
        )
    )
    return result.scalars().first()


async def _upsert_header(
    session: AsyncSession,
    *,
    existing: SupplierStatement | None,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    extracted: ExtractedStatement,
    paperless_document_id: int,
    status: str,
    extraction_meta: dict,
    our_ap_as_at: Decimal | None,
    balance_delta: Decimal | None,
) -> SupplierStatement:
    if existing is not None:
        stmt = existing
    else:
        stmt = SupplierStatement(
            tenant_id=tenant_id,
            company_id=company_id,
        )
        session.add(stmt)

    stmt.source_document_id = paperless_document_id
    stmt.tenant_id = tenant_id
    stmt.company_id = company_id
    stmt.supplier_name = extracted.supplier_name
    stmt.supplier_abn = extracted.supplier_abn
    stmt.customer_ref = extracted.customer_ref
    stmt.statement_date = extracted.statement_date
    stmt.terms = extracted.terms
    stmt.opening_balance = extracted.opening_balance
    stmt.closing_balance = extracted.closing_balance
    stmt.status = status
    stmt.extraction_meta = extraction_meta
    stmt.our_ap_as_at = our_ap_as_at
    stmt.balance_delta = balance_delta
    return stmt


async def _upsert_lines(
    session: AsyncSession,
    stmt: SupplierStatement,
    extracted: ExtractedStatement,
) -> list[SupplierStatementLine]:
    """Replace all statement lines with freshly extracted ones.

    For existing statements: DELETE all prior lines via SQL (avoids touching
    the lazy-loaded relationship collection), then expire the attribute so
    SQLAlchemy reloads it cleanly after we add the new rows.

    For new statements: collection is already empty; just add rows.

    Returns the newly created line objects (already added to session).
    """
    from sqlalchemy import delete as sa_delete

    if stmt.id is not None:
        # DELETE all prior lines in the DB without touching the ORM collection
        # (avoids MissingGreenlet from lazy-loading a relationship outside greenlet).
        await session.execute(
            sa_delete(SupplierStatementLine).where(
                SupplierStatementLine.statement_id == stmt.id
            )
        )
        # Expire the relationship so SQLAlchemy reloads it on next access
        # rather than serving a stale in-memory collection.
        session.expire(stmt, ["lines"])

    new_lines: list[SupplierStatementLine] = []
    for el in extracted.lines:
        line = SupplierStatementLine(
            tenant_id=stmt.tenant_id,
            statement_id=stmt.id,
            line_date=el.line_date,
            line_type=el.line_type,
            reference=el.reference,
            description=el.description,
            amount=el.amount,
            match_status=StatementMatchStatus.UNMATCHED.value,
        )
        session.add(line)
        new_lines.append(line)

    return new_lines


async def _resolve_contact(
    session: AsyncSession,
    company_id: uuid.UUID,
    supplier_name: str,
) -> Contact | None:
    """Exact case-insensitive name match against supplier/both contacts."""
    result = await session.execute(
        select(Contact).where(
            Contact.company_id == company_id,
            Contact.contact_type.in_([ContactType.SUPPLIER, ContactType.BOTH]),
            Contact.name.ilike(supplier_name),
        )
    )
    return result.scalars().first()


async def _load_supplier_bills(
    session: AsyncSession,
    company_id: uuid.UUID,
    contact_id: uuid.UUID | None,
) -> list[Bill]:
    """Load POSTED + DRAFT bills for the resolved contact.

    If contact_id is None (supplier not resolved), return empty list —
    reconcile_lines will mark all statement lines as MISSING_IN_BOOKS which
    is honest (we can't match without a contact).
    """
    if contact_id is None:
        return []
    result = await session.execute(
        select(Bill).where(
            Bill.company_id == company_id,
            Bill.contact_id == contact_id,
            Bill.status.in_([BillStatus.POSTED, BillStatus.DRAFT]),
        )
    )
    return list(result.scalars().all())
