import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models.business_identifier import BusinessIdentifier


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
    # ``abn`` is no longer a column — it is a read-through hybrid over the
    # ``au_abn`` business identifier (see the ``identifiers`` relationship and
    # the ``abn`` hybrid_property at the end of the class). The column was
    # dropped in migration 0204; ``business_identifiers`` is the single source
    # of truth for every registration number. Writes go through
    # ``services.business_identifiers.upsert`` (the company service routes the
    # legacy ``abn`` field there). Non-AU registry codes (e.g. the Estonian
    # äriregistri kood, ``ee_regcode``) are read explicitly via their own
    # scheme — this AU-specific accessor deliberately does not overload them.
    # ``acn`` is no longer a column — it is a read-through hybrid over the
    # ``au_acn`` business identifier (see the ``acn`` hybrid_property at the end
    # of the class). The column was dropped in migration 0205, mirroring the
    # ``abn`` clean-move in 0204; ``business_identifiers`` is the single source
    # of truth. Writes route through ``services.companies`` (the ``acn`` field
    # is upserted as the ``au_acn`` identifier).
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
    # Presentation/base currency. Defaults to the jurisdiction-neutral
    # ISO-4217 "no currency" sentinel ("XXX") so the bare core names no
    # national currency — a concrete currency (AUD, GBP, EUR…) is supplied
    # explicitly at company creation (the API/CompanyCreate contract still
    # defaults to AUD; the seed company sets it from config).
    base_currency: Mapped[str] = mapped_column(String(3), default="XXX", nullable=False)
    coa_template_key: Mapped[str] = mapped_column(
        String(64), default="xx/default", nullable=False,
        comment="Jurisdiction CoA/reference-data template key",
    )
    # Multi-jurisdiction routing key. Free text (no FK) because the
    # jurisdiction registry lives in the reference DB; validated at the
    # service layer. Defaults to the neutral sentinel "XX" (zero bolt-on
    # jurisdiction modules) so the core is jurisdiction-agnostic — a
    # concrete jurisdiction (AU/NZ/UK/EE/…) is chosen explicitly at company
    # creation from the loaded module set. See docs/multi-jurisdiction.md.
    jurisdiction: Mapped[str] = mapped_column(String(3), default="XX", nullable=False)
    # M1.5 · T4 — legal-entity / business-structure type. Free text (no FK)
    # because the entity_structure_types registry lives in the reference DB;
    # validated at the service layer against this company's jurisdiction,
    # exactly like ``jurisdiction`` above. NULL = not yet classified. Values
    # are RefEntityStructureType.code (e.g. 'pty_ltd', 'disc_trust', 'smsf').
    entity_structure_code: Mapped[str | None] = mapped_column(String(32))
    # M1.5 · T10b — statutory chart-of-accounts framework the company reports
    # under (SKR03/SKR04, PCG, ...). Free text (no FK) because the
    # statutory_account_frameworks registry lives in the reference DB;
    # validated at the service layer against this company's jurisdiction,
    # exactly like ``entity_structure_code`` above. NULL = none / not
    # applicable — which is every AU company, since Australia mandates no
    # account numbering plan.
    statutory_framework_code: Mapped[str | None] = mapped_column(String(32))
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

    tax_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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

    # AR/AP control-account override (0198, Packet 4b). Every posting site
    # historically hardcoded the AU chart-of-accounts convention codes
    # ("1-1200" Trade Debtors / "2-1200" Trade Creditors) — see
    # ``saebooks.services.control_accounts`` for the single resolver every
    # call site now shares. NULL = engine falls back to the AU codes, so
    # every existing (AU) company is byte-identical. A non-AU company whose
    # chart uses different control-account codes sets these instead of the
    # engine forcing an AU-shaped chart on it.
    ar_control_account_code: Mapped[str | None] = mapped_column(String(64))
    ap_control_account_code: Mapped[str | None] = mapped_column(String(64))

    # EE payroll GL control-account overrides (0200, Fixer round 4 F1).
    # Previously resolved from a GLOBAL ``Setting`` row keyed only by
    # ``key`` (no company scoping at all) — two EE companies on one
    # instance could not configure these independently and a code
    # collision would silently mispost. Same NULL-raises-loudly contract
    # as before (see ``pay_runs_v2._account_by_company_column``); unlike
    # AR/AP there is no AU-convention default to fall back to.
    ee_payroll_wages_expense_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_social_tax_expense_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_unemployment_employer_expense_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_income_tax_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_unemployment_employee_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_pillar_ii_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_social_tax_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_unemployment_employer_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_net_pay_clearing_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_fringe_benefit_income_tax_expense_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_fringe_benefit_social_tax_expense_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_fringe_benefit_income_tax_payable_account_code: Mapped[str | None] = mapped_column(String(64))
    ee_payroll_fringe_benefit_social_tax_payable_account_code: Mapped[str | None] = mapped_column(String(64))

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

    # Every registration number this company holds, one row per scheme
    # (au_abn, au_acn, ee_regcode, uk_crn, ...). ``lazy="selectin"`` so the
    # collection is always eager-loaded alongside the company — the ``abn``
    # hybrid below reads it synchronously, including from the sync PDF/context
    # builders that receive a company but no session. The DB owns cascade
    # (business_identifiers.company_id is ON DELETE CASCADE); the ORM keeps a
    # plain read-mostly view (writes go through services.business_identifiers).
    identifiers: Mapped[list["BusinessIdentifier"]] = relationship(
        "BusinessIdentifier",
        lazy="selectin",
        passive_deletes=True,
    )

    @hybrid_property
    def abn(self) -> str | None:
        """The Australian Business Number — the value of the ``au_abn``
        business identifier, or None. Read-through sugar over
        ``identifiers``; the physical ``abn`` column was dropped in 0204.
        This is AU-specific by design: a non-AU registry code (e.g. the
        Estonian äriregistri kood) is read via its own scheme, never here."""
        for ident in self.identifiers:
            if ident.scheme == "au_abn":
                return ident.value
        return None

    @abn.inplace.expression
    @classmethod
    def _abn_expression(cls):
        """SQL form: correlated scalar subquery on the ``au_abn`` identifier.
        Defensive — the engine resolves the abn in Python via the getter; this
        only serves the rare ``select(Company.abn)`` / filter-on-abn path."""
        return (
            select(BusinessIdentifier.value)
            .where(
                BusinessIdentifier.company_id == cls.id,
                BusinessIdentifier.scheme == "au_abn",
            )
            .scalar_subquery()
        )

    @hybrid_property
    def acn(self) -> str | None:
        """The Australian Company Number — the value of the ``au_acn``
        business identifier, or None. Read-through sugar over ``identifiers``;
        the physical ``acn`` column was dropped in 0205 (same clean-move as
        ``abn`` in 0204). AU-specific by design."""
        for ident in self.identifiers:
            if ident.scheme == "au_acn":
                return ident.value
        return None

    @acn.inplace.expression
    @classmethod
    def _acn_expression(cls):
        """SQL form: correlated scalar subquery on the ``au_acn`` identifier.
        Defensive — the engine resolves the acn in Python via the getter."""
        return (
            select(BusinessIdentifier.value)
            .where(
                BusinessIdentifier.company_id == cls.id,
                BusinessIdentifier.scheme == "au_acn",
            )
            .scalar_subquery()
        )

    @hybrid_property
    def registrikood(self) -> str | None:
        """The Estonian business-registry code (äriregistri kood) — the
        value of the ``ee_regcode`` business identifier, or None.
        Read-through sugar over ``identifiers``, exactly mirroring
        ``abn``/``acn`` over their AU schemes; there is no physical
        ``registrikood`` column (business_identifiers is the sole source of
        truth). Writes route through ``services.companies`` (the
        ``registrikood`` field is upserted as the ``ee_regcode`` identifier).
        EE-specific by design — None on every non-EE company."""
        for ident in self.identifiers:
            if ident.scheme == "ee_regcode":
                return ident.value
        return None

    @registrikood.inplace.expression
    @classmethod
    def _registrikood_expression(cls):
        """SQL form: correlated scalar subquery on the ``ee_regcode``
        identifier — serves ``select(Company.registrikood)`` / filter-on-
        registrikood (the duplicate-detection test path)."""
        return (
            select(BusinessIdentifier.value)
            .where(
                BusinessIdentifier.company_id == cls.id,
                BusinessIdentifier.scheme == "ee_regcode",
            )
            .scalar_subquery()
        )

    @hybrid_property
    def kmv_number(self) -> str | None:
        """The Estonian VAT number ("käibemaksukohustuslase number" / KMV)
        — the value of the ``ee_vat`` business identifier, or None.
        Read-through sugar over ``identifiers`` (the ``registrikood``
        precedent above); no physical column. EE-specific by design."""
        for ident in self.identifiers:
            if ident.scheme == "ee_vat":
                return ident.value
        return None

    @kmv_number.inplace.expression
    @classmethod
    def _kmv_number_expression(cls):
        """SQL form: correlated scalar subquery on the ``ee_vat`` identifier."""
        return (
            select(BusinessIdentifier.value)
            .where(
                BusinessIdentifier.company_id == cls.id,
                BusinessIdentifier.scheme == "ee_vat",
            )
            .scalar_subquery()
        )
