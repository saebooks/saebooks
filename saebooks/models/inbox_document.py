"""Document Inbox — captured documents awaiting review/publish.

An ``InboxDocument`` is one captured source document (photographed
receipt, uploaded file, emailed supplier-invoice attachment): a
tenant-scoped row pointing at an unlinked saebooks-vault blob
(``vault_file_id`` — the engine stores no bytes), carrying the AI
extraction result, the review state machine, and — once published —
provenance of the DRAFT record it became.

Conventions (mirrors ``models/bank_statement.py``):

* Statuses are ``enum.StrEnum`` in Python, TEXT + CHECK in Postgres
  (migration 0174) — never a Postgres enum.
* ``version`` is the optimistic lock (bank_statement_lines precedent).
* ``extract`` is the verbatim model output and is IMMUTABLE once
  written; reviewer edits live in ``extraction_override`` only.

Deliberately NOT ``CompanyScoped``: ``company_id`` is nullable
(documents arrive unrouted; a company is required only at publish), and
the CompanyScoped loader criteria would hide NULL-company rows from the
inbox list. Isolation is tenant-level: RLS (ENABLE + FORCE +
``tenant_isolation`` policy, migration 0174) plus explicit app-layer
``tenant_id`` filters on every query.
"""
import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class InboxDocumentSource(enum.StrEnum):
    UPLOAD = "UPLOAD"
    EMAIL = "EMAIL"
    API = "API"


class InboxDocumentStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"
    FAILED = "FAILED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class ExtractionConfidence(enum.StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"


class PublishedRecordKind(enum.StrEnum):
    EXPENSE = "EXPENSE"
    BILL = "BILL"
    CREDIT_NOTE = "CREDIT_NOTE"


class RejectReason(enum.StrEnum):
    DUPLICATE = "DUPLICATE"
    NOT_A_DOCUMENT = "NOT_A_DOCUMENT"
    PERSONAL = "PERSONAL"
    OTHER = "OTHER"


class InboxDocument(Base):
    __tablename__ = "inbox_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        comment="NULL until routed/assigned; required at publish",
    )
    vault_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Unlinked vault blob — the engine stores no bytes",
    )
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[InboxDocumentSource] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(
        Text,
        comment="EMAIL: '<rfc5322-message-id>#<attachment-index>'",
    )
    status: Mapped[InboxDocumentStatus] = mapped_column(
        Text,
        nullable=False,
        default=InboxDocumentStatus.RECEIVED,
        server_default="RECEIVED",
    )
    extract: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Verbatim model output — IMMUTABLE once written",
    )
    extraction_override: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        comment="Reviewer edits live here, never in extract",
    )
    extract_model: Mapped[str | None] = mapped_column(String(80))
    extraction_confidence: Mapped[ExtractionConfidence | None] = mapped_column(Text)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sweep machinery — in schema from day one (used from phase 3).
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_documents.id", ondelete="SET NULL"),
    )
    # Supplier-rule suggestions (migration 0175, phase 2) — filled at
    # extraction time when a rule matches; suggestion-only, never
    # auto-published. The reviewer's confirmed values live in
    # extraction_override / the publish payload, not here.
    suggested_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
    )
    suggested_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
    )
    suggested_tax_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_codes.id", ondelete="SET NULL"),
    )
    supplier_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("supplier_rules.id", ondelete="SET NULL"),
    )
    published_record_kind: Mapped[PublishedRecordKind | None] = mapped_column(Text)
    published_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        comment="Polymorphic across expenses/bills/credit_notes — no FK",
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[RejectReason | None] = mapped_column(Text)
    reject_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
