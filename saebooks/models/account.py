import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.db_types import Money
from saebooks.models._scope import CompanyScoped


class AccountType(enum.StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    OTHER_INCOME = "OTHER_INCOME"
    EXPENSE = "EXPENSE"
    COST_OF_SALES = "COST_OF_SALES"
    OTHER_EXPENSE = "OTHER_EXPENSE"


class NetAssetRestrictionTier(enum.StrEnum):
    """Fund-accounting net-asset restriction tiers (M1.5 · T10b). Vocabulary
    for ``Account.net_asset_restriction_tier`` — stored as a plain string
    (service-layer validation, like ``chart_template.account_type``)."""

    UNRESTRICTED = "unrestricted"
    BOARD_DESIGNATED = "board_designated"
    DONOR_RESTRICTED_TEMPORARY = "donor_restricted_temporary"
    DONOR_RESTRICTED_PERMANENT = "donor_restricted_permanent"


NET_ASSET_RESTRICTION_TIERS = tuple(t.value for t in NetAssetRestrictionTier)


class Account(CompanyScoped, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_accounts_company_code"),
        # Composite-FK target for the per-company money-movement models:
        # transfers.(from/to_account_id, company_id) and
        # receipts.(bank_account_id, company_id) FK to accounts(id, company_id)
        # so a transfer/receipt can never point at a sister company's account.
        # On Postgres this constraint is created by migration 0152 (raw SQL);
        # it was missing from the ORM, so SQLite's bootstrap_schema
        # (Base.metadata.create_all) never emitted the unique index and the
        # composite FKs rejected every insert with "foreign key mismatch" —
        # the transfers + receipts web UIs were dead on Community/SQLite.
        # Declaring it here creates it on SQLite bootstrap and keeps the ORM
        # in lock-step with the Postgres schema (no new migration: Postgres
        # already has it from 0152).
        UniqueConstraint("id", "company_id", name="uq_accounts_id_company"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type_enum"), nullable=False
    )
    tax_code_default: Mapped[str | None] = mapped_column(String)
    is_header: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reconcile: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    system_managed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="System-managed accounts (GST, etc.) — auto-posted by the engine",
    )
    is_trust_account: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="NSW Property Act: designates a trust bank account; commingling guard enforced on post",
    )
    show_on_invoice: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False,
        comment=(
            "Remit-to designation: this bank account's BSB/number/title feed the "
            "How-to-Pay panel on invoice + credit-note PDFs; single flag per company "
            "(service layer clears siblings)"
        ),
    )
    # ABA / Direct Entry — populated only on bank accounts. Remitter
    # side of the APCA agreement: BSB + account + account title, plus
    # the sponsor bank's 6-digit User ID and 3-letter abbreviation.
    bsb: Mapped[str | None] = mapped_column(
        String(7), comment="BSB formatted 'xxx-xxx' (ABA remitter)"
    )
    bank_account_number: Mapped[str | None] = mapped_column(String(9))
    bank_account_title: Mapped[str | None] = mapped_column(
        String(32), comment="Account title on the bank statement (ABA field)"
    )
    apca_user_id: Mapped[str | None] = mapped_column(
        String(6), comment="6-digit Direct Entry User ID from sponsor bank"
    )
    bank_abbreviation: Mapped[str | None] = mapped_column(
        String(3), comment="3-letter ABA bank code — CBA, ANZ, NAB, WBC, …"
    )
    # account_kind classifies bank-side accounts so credit cards,
    # loans, and cash all appear in the /bank-accounts list and on
    # the dashboard. NULL for non-bank ledger accounts.
    #
    # Backed by the Postgres enum type ``account_kind_enum`` (created
    # by an earlier deploy on 2026-05-20 and re-asserted by alembic
    # 0119 idempotently). ``create_type=False`` tells SQLAlchemy not
    # to try to CREATE TYPE on metadata.create_all() — the type
    # already exists; we just need the SELECT/WHERE/INSERT bind
    # parameters to typecast to the enum so Postgres doesn't reject
    # them with ``operator does not exist: account_kind_enum = varchar``.
    account_kind: Mapped[str | None] = mapped_column(
        Enum(
            "BANK_CHECKING",
            "BANK_SAVINGS",
            "CREDIT_CARD",
            "BANK_LOAN",
            "CASH",
            "OTHER",
            name="account_kind_enum",
            create_type=False,
            native_enum=True,
        ),
        nullable=True,
        comment="One of BANK_CHECKING / BANK_SAVINGS / CREDIT_CARD / BANK_LOAN / CASH / OTHER",
    )
    # Credit limit — populated on bank-side accounts that have one (chiefly
    # CREDIT_CARD, optionally BANK_LOAN). NULL means no limit set.
    # ``credit_limit_kind`` mirrors the soft/hard precedent of SeatCapKind in
    # services/licence/caps.py: soft (default) warns when exceeded but never
    # blocks data entry; hard is a stronger state. Backed by a CHECK
    # constraint (ck_accounts_credit_limit_kind) added in migration 0141.
    credit_limit: Mapped[Decimal | None] = mapped_column(
        Money(),
        nullable=True,
        comment="Credit limit for this account; NULL = no limit set",
    )
    credit_limit_kind: Mapped[str | None] = mapped_column(
        String(4),
        server_default="soft",
        nullable=True,
        comment="soft (warn only) | hard — mirrors SeatCapKind soft/hard",
    )
    # M1.5 · T10b — statutory chart-of-accounts mapping (all nullable; AU
    # accounts stay NULL because Australia mandates no numbering plan or
    # local-language labels). Populated for companies reporting under a
    # mandated framework (companies.statutory_framework_code): the account's
    # number in that framework, its local-language statutory label, and the
    # framework class/group it rolls up into. Reference-data only — nothing
    # in the posting path reads these.
    statutory_account_code: Mapped[str | None] = mapped_column(
        String(32),
        comment="Account number under the company's statutory framework, e.g. SKR03 '4400'",
    )
    statutory_account_label_local: Mapped[str | None] = mapped_column(
        String(255),
        comment="Local-language statutory label, e.g. 'Erlöse 19 % USt'",
    )
    statutory_parent_class: Mapped[str | None] = mapped_column(
        String(64),
        comment="Statutory class/group the account sits under, e.g. 'Klasse 4'",
    )
    # M1.5 · T10b — NFP / fund-accounting net-asset restriction tier. NULL
    # for for-profit books (every existing account). One of
    # NET_ASSET_RESTRICTION_TIERS when the entity tracks donor restrictions
    # (canonical_bucket 'nonprofit'/'government' structures).
    net_asset_restriction_tier: Mapped[str | None] = mapped_column(
        String(32),
        comment=(
            "One of unrestricted / board_designated / donor_restricted_temporary / "
            "donor_restricted_permanent; NULL = not fund-accounted"
        ),
    )
    # M1.5 P1 tail — current/non-current balance-sheet classification. The
    # AU CoA seed source carries this distinction (Odoo account_type values
    # asset_current/asset_non_current/liability_current/liability_non_current)
    # but previously collapsed it into the flat ASSET/LIABILITY account_type
    # above (seed/load_au_coa.py). NULL for non-AU-seeded accounts, headers,
    # and non-asset/liability account types (equity/income/expense have no
    # current/non-current distinction). One of "current" / "non_current"
    # when populated. Reference-data only — nothing in the posting path
    # reads this column.
    balance_sheet_classification: Mapped[str | None] = mapped_column(
        String(16),
        comment="One of current / non_current; NULL = not classified (headers, equity/income/expense)",
    )
    # M1.5 P1 tail — contra-account designation + normal balance. A contra
    # account (e.g. Accumulated Depreciation, a contra-ASSET) carries the
    # opposite normal balance to its account_type's usual side.
    # ``normal_balance`` is "debit" or "credit"; NULL = not classified.
    # Reference-data only — nothing in the posting path reads these.
    is_contra: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
        comment="True for contra accounts (e.g. Accumulated Depreciation) — opposite normal balance to account_type",
    )
    normal_balance: Mapped[str | None] = mapped_column(
        String(6),
        comment="One of debit / credit; NULL = not classified",
    )
    # M1.5 P1 tail — for-profit equity sub-classification. AccountType has
    # a single EQUITY value; this column carries the finer breakdown
    # (share_capital / retained_earnings / reserves / drawings / other).
    # NULL for non-equity accounts and unclassified equity accounts.
    # Reference-data only — nothing in the posting path reads this.
    equity_subtype: Mapped[str | None] = mapped_column(
        String(32),
        comment="One of share_capital / retained_earnings / reserves / drawings / other; NULL = unclassified",
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Optimistic-locking version — bumped on every write through the API.
    # Jinja routes that call the service layer without expected_version skip
    # the guard (last-writer-wins, same behaviour as before Phase 1).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
