import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class CostingMethod(enum.StrEnum):
    """Per-company inventory costing policy (Wave D, 2026-07-10).

    Richard's decision (2): costing method is a per-company SETTING —
    the client chooses; the engine never forces one method. Stored as a
    scalar column on ``companies`` alongside the other per-company
    policy switches (``bookkeeping_mode`` / ``audit_mode`` /
    ``writeoff_mode``) because it is a company-wide policy, not a
    per-item attribute.

    * ``WEIGHTED_AVERAGE`` — the pre-Wave-D behaviour (WAC blend on
      receive, COGS at the running average on issue). DEFAULT so every
      existing company and all existing WAC tests are unaffected.
    * ``FIFO`` — perpetual first-in-first-out cost layers: a receipt
      creates a layer; an issue consumes layers oldest-first and posts
      COGS from the consumed layers.
    * ``QUANTITY_ONLY`` — track on-hand quantity + movements only; NO
      automatic COGS / stock-valuation journal is posted (cost stays
      whatever the bills recorded).
    """

    WEIGHTED_AVERAGE = "weighted_average"
    FIFO = "fifo"
    QUANTITY_ONLY = "quantity_only"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String)
    trading_name: Mapped[str | None] = mapped_column(String)
    abn: Mapped[str | None] = mapped_column(String(20))
    acn: Mapped[str | None] = mapped_column(String(20))
    # Remittance / "How to Pay" details — rendered on the invoice PDF (0168).
    # All nullable; NULL = nothing shown (template guards on bank_account_number).
    bank_name: Mapped[str | None] = mapped_column(String)
    bank_bsb: Mapped[str | None] = mapped_column(String)
    bank_account_number: Mapped[str | None] = mapped_column(String)
    bank_account_name: Mapped[str | None] = mapped_column(String)
    payment_terms_text: Mapped[str | None] = mapped_column(String)
    terms_url: Mapped[str | None] = mapped_column(String)
    # Letterhead contact details (0171) — rendered under the address in the
    # PDF document header on invoices, bills and credit notes. All nullable;
    # NULL = the line is simply omitted (template guards per-field).
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    website: Mapped[str | None] = mapped_column(String)
    # Company-default free-text payment terms (0171) — copied onto invoices /
    # credit notes at CREATE when the payload doesn't supply payment_terms
    # (per-document override wins). Distinct from payment_terms_text (0168
    # standing Terms-of-Trade fine print) and per-contact due-date terms (0165).
    default_payment_terms: Mapped[str | None] = mapped_column(Text)
    address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    base_currency: Mapped[str] = mapped_column(String(3), default="AUD", nullable=False)
    coa_template_key: Mapped[str] = mapped_column(
        String(64), default="au/default", nullable=False,
        comment="Jurisdiction CoA/reference-data template key",
    )
    # Multi-jurisdiction routing key. Free text (no FK) because the
    # jurisdiction registry lives in the reference DB; validated at the
    # service layer. Defaults to AU because that is the only jurisdiction
    # wired end-to-end at v0.1.4 (see docs/multi-jurisdiction.md).
    jurisdiction: Mapped[str] = mapped_column(String(3), default="AU", nullable=False)
    # M1.5 · T4 — legal-entity / business-structure type. Free text (no FK)
    # because the entity_structure_types registry lives in the reference DB;
    # validated at the service layer against this company's jurisdiction,
    # exactly like ``jurisdiction`` above. NULL = not yet classified. Values
    # are RefEntityStructureType.code (e.g. 'pty_ltd', 'disc_trust', 'smsf').
    entity_structure_code: Mapped[str | None] = mapped_column(String(32))
    fin_year_start_month: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    audit_mode: Mapped[str] = mapped_column(String, default="immutable", nullable=False)

    # Per-company SISS credentials (Batch II, Enterprise-gated via
    # FLAG_PER_COMPANY_SISS). NULL on any field = fall back to env-var
    # creds (pre-Batch-II behaviour). ``*_encrypted`` columns are Fernet
    # ciphertext produced by ``saebooks.services.crypto.encrypt_field`` —
    # never persist plaintext here. ``siss_environment`` is free-text
    # (``production`` / ``sandbox``) routed by the resolver.
    siss_client_id: Mapped[str | None] = mapped_column(String(128))
    siss_client_secret_encrypted: Mapped[str | None] = mapped_column(String)
    siss_subscription_key_encrypted: Mapped[str | None] = mapped_column(String)
    siss_environment: Mapped[str | None] = mapped_column(String(32))

    gst_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gst_effective_date: Mapped[date | None] = mapped_column(Date)

    # PSI (Personal Services Income) classification — ATO requirement for contractors.
    # "unsure" triggers a dashboard reminder to classify.
    psi_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unsure")

    # Bad-debt write-off & recovery policy (per-company settings). The ledger
    # postings live in the engine bad_debt service; these columns only hold the
    # bookkeeping *policy* the web app drives off. Validated at the schema /
    # service layer (see CompanyUpdate). Defaults per design spec:
    #   writeoff_mode             review | auto | manual         (default review)
    #   writeoff_threshold_days   positive int                   (default 90)
    #   recovery_mode             smart_prompt | manual | reopen (default smart_prompt)
    #   bad_debt_recovery_account optional account code/id (NULL = engine resolves
    #                             4-1290 Bad Debt Recovery on demand)
    writeoff_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="review", server_default="review"
    )
    writeoff_threshold_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    recovery_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="smart_prompt", server_default="smart_prompt"
    )
    bad_debt_recovery_account: Mapped[str | None] = mapped_column(String(64))

    # Cashbook edition (single-entry UX over double-entry storage). See
    # docs/cashbook-edition-design.md. ``bookkeeping_mode`` flips the UX
    # surface; the underlying ledger is always double-entry. CHECK
    # constraint at the DB layer refuses ``cashbook`` mode without a
    # default bank account set.
    bookkeeping_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full"
    )
    cashbook_default_bank_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    cashbook_categories: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Inventory costing policy (Wave D, 2026-07-10). Per-company setting —
    # the client chooses; the engine never forces a method. Values are
    # ``CostingMethod`` (weighted_average | fifo | quantity_only). Default
    # ``weighted_average`` preserves the pre-Wave-D behaviour for every
    # existing company (the FLAG_INVENTORY module was WAC-locked before).
    # A DB CHECK constraint (migration 0185) is the second line of defence
    # behind this Python-validated column.
    costing_method: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=CostingMethod.WEIGHTED_AVERAGE,
        server_default=CostingMethod.WEIGHTED_AVERAGE.value,
    )

    # Legal-entity model (migration 0133, 2026-05-24).
    # entity_type: COMPANY | TRUST | INDIVIDUAL | PARTNERSHIP | SUPER_FUND
    # trades: false for pure trustee companies that hold no ABN
    # trustee_company_id: on a TRUST row, points at the trustee Company
    # entity_type is a Postgres ENUM (``entity_type_enum``) created by
    # migration 0133, NOT a varchar. Mapping it as String(32) made asyncpg
    # bind the parameter as ``$n::VARCHAR``, which Postgres refuses to cast
    # implicitly to the enum type ("column is of type entity_type_enum but
    # expression is of type character varying") — every create_company 500'd.
    # ``create_type=False`` because the type already exists in every deployed
    # DB; SQLAlchemy must reference it, never try to CREATE TYPE it.
    # ``native_enum=True`` + string-valued labels keep the Python interface a
    # plain str ("COMPANY" etc.) so callers and serialisation are unchanged.
    entity_type: Mapped[str] = mapped_column(
        Enum(
            "COMPANY",
            "TRUST",
            "INDIVIDUAL",
            "PARTNERSHIP",
            "SUPER_FUND",
            name="entity_type_enum",
            native_enum=True,
            create_type=False,
            validate_strings=True,
        ),
        nullable=False,
        server_default="COMPANY",
        default="COMPANY",
    )
    trades: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True,
    )
    trustee_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # Optimistic-locking version — bumped on every write through the API.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
