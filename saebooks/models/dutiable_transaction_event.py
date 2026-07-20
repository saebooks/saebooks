"""Dutiable transaction event — a postable stamp/transfer/conveyance/
securities/insurance duty (M1.5 · T5).

Before this table ``duty_rate_schedule`` (reference DB; renamed from
``stamp_duty_rate`` — M1.5 Wave 3a) was a rate-lookup table only —
nothing recorded that a jurisdiction actually assessed duty
on a real transaction, and nothing posted a journal for it. This table is
the missing company-scoped economic EVENT, the same relationship
``Transfer`` has to a plain account-to-account movement: one row per
assessed duty, linked to exactly one posted journal entry via
``jurisdictions.au.dutiable_events.create_and_post_event`` (NEVER a hand-authored
journal entry — same posting chokepoint every other record type uses,
``journal.post_in_txn``).

``jurisdiction`` / ``sub_jurisdiction`` use the T3 jurisdiction hierarchy
(``jurisdictions.parent_code`` / ``level``, migration
``0002_jurisdiction_hierarchy`` in the reference DB) — ``jurisdiction`` is
the country-level code (e.g. ``AUS``), ``sub_jurisdiction`` is an optional
state/province-level child code (e.g. ``AUQ`` for Queensland, following
the ``AUQ`` convention from ``tests/seeds/test_jurisdiction_hierarchy.py``).
Both are free-text ``String(3)``, non-FK — the reference DB is a separate
database with no cross-DB foreign key (same posture as
``Company.jurisdiction`` and ``Company.entity_structure_code``); a caller
that wants hierarchy validation resolves it at the service layer against
the reference DB when configured.

``applied_concession_id`` is an opaque nullable UUID pointing at a
``RefDutyConcession`` row (reference DB) — also non-FK for the same
cross-DB reason. ``computed_duty`` is caller-supplied or derived via
``jurisdictions.au.dutiable_events.lookup_stamp_duty_rate`` reading the existing
``duty_rate_schedule`` reference table for AU real-property duty; this
table does not own or require reference-DB rate data to exist.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T5).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class DutyType(enum.StrEnum):
    """Kind of dutiable transaction. Mirrors ``duty_rate_schedule.transaction_type``
    (kept as a plain string there, per its module docstring) so a lookup can
    map one to the other; not every ``DutyType`` has a matching rate row yet.
    """

    PROPERTY_TRANSFER = "property_transfer"  # real-property / conveyance duty
    MOTOR_VEHICLE = "motor_vehicle"
    INSURANCE = "insurance"
    MORTGAGE = "mortgage"
    SECURITIES = "securities"  # share/unit transfer duty
    LEASE = "lease"  # lease/tenancy grant duty (rent or premium base)
    # indirect transfer: acquiring a significant interest in a land-rich
    # entity, dutiable per the landholder_duty_rules reference catalog
    LANDHOLDER_ACQUISITION = "landholder_acquisition"


class DutiableEventStatus(enum.StrEnum):
    """Lifecycle of a dutiable transaction event.

    Stored as ``String(16)`` (mirrors ``TransferStatus`` — a Python
    StrEnum persisted as a plain string, no DB enum type).
    """

    POSTED = "POSTED"      # the linked JE is posted
    REVERSED = "REVERSED"  # the linked JE has been reversed


_STATUS_LEN = 16
_DUTY_TYPE_LEN = 32
_JURISDICTION_LEN = 3


class DutiableTransactionEvent(CompanyScoped, Base):
    """One assessed duty on one dutiable transaction, owned by ``company_id``.

    The double-entry lives on the linked ``JournalEntry``
    (``journal_entry_id``); this row is the durable, queryable record that
    gives the assessment a stable identity and an audit trail back to the
    jurisdiction, duty type, dutiable value and any concession applied.
    """

    __tablename__ = "dutiable_transaction_events"
    __table_args__ = (
        # debit_account_id / credit_account_id must each be an account OF
        # company_id — same composite-FK posture as Transfer
        # (0155/transfers.py) so an event can never reference a sister
        # company's account.
        ForeignKeyConstraint(
            ["debit_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_dutiable_txn_events_debit_account_company",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["credit_account_id", "company_id"],
            ["accounts.id", "accounts.company_id"],
            name="fk_dutiable_txn_events_credit_account_company",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    duty_type: Mapped[str] = mapped_column(String(_DUTY_TYPE_LEN), nullable=False)
    # Country-level jurisdiction code, e.g. 'AUS'. Free-text, non-FK — see
    # module docstring (reference DB is a separate database).
    jurisdiction: Mapped[str] = mapped_column(
        String(_JURISDICTION_LEN), nullable=False
    )
    # Optional state/province-level child jurisdiction code, e.g. 'AUQ'
    # (Queensland) — a child of ``jurisdiction`` via the T3 hierarchy
    # (``jurisdictions.parent_code``). Free-text, non-FK.
    sub_jurisdiction: Mapped[str | None] = mapped_column(
        String(_JURISDICTION_LEN)
    )
    dutiable_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    computed_duty: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Foreign/non-resident purchaser surcharge component INCLUDED in
    # computed_duty (informational breakdown — the JE still posts
    # computed_duty as one amount). NULL = no surcharge applied.
    surcharge_duty: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # Opaque pointer at a RefDutyConcession row (reference DB) — non-FK for
    # the same cross-DB reason as jurisdiction/sub_jurisdiction above.
    applied_concession_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    # Opaque pointer at the RefDutySurchargeRate row (reference DB) the
    # surcharge component was computed from — non-FK, same cross-DB posture.
    applied_surcharge_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(_STATUS_LEN),
        nullable=False,
        default=DutiableEventStatus.POSTED,
        server_default=DutiableEventStatus.POSTED.value,
    )
    # Debited on post — the duty cost (EXPENSE account) or the asset the
    # duty is capitalised into (ASSET account); validated at the service
    # layer, not restricted by account type here (duty may be expensed or
    # capitalised depending on company policy).
    debit_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # Credited on post — the payable/payment account the duty is owed to
    # or paid from.
    credit_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # Linkage to the posted JE this event compiled to. RESTRICT (mirrors
    # Transfer/0155) so the JE can never be hard-deleted out from under its
    # event; unwind goes through reversal, never delete.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
