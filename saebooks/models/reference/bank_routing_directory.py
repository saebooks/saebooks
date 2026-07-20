"""Bank/branch routing-number directory (M1.5 · Wave 3a rename hygiene).

Renamed from ``bsb_directory`` — zero consumers. ``bank_routing_identifier``
(T10, ``models/bank_routing_identifier.py``) is the OWNER side (a company's
own account carries a routing identifier + scheme); this table is the
lookup DIRECTORY side (resolving a routing number to the institution that
issued it — AU BSB today, IBAN/ABA/sort-code directories per-jurisdiction
later), so it is not subsumed by T10 and gets a generic name instead of
being dropped.
"""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class BankRoutingDirectoryEntry(ReferenceBase):
    __tablename__ = "bank_routing_directory"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bsb: Mapped[str] = mapped_column(String(6), nullable=False, unique=True)
    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(256))
    suburb: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(8))
    # M1.5 · 5-SUBJURIS (reference migration 0016): FK promotion of the
    # ad-hoc ``state`` address string into the T3 jurisdiction tree.
    # NULLABLE and additive — ``state`` stays authoritative for existing
    # callers during the transition. The table has no ``jurisdiction``
    # column (it is BSB/AU-only today), so the backfill maps 'QLD' →
    # 'AU-QLD' under the 'AUS' parent.
    sub_jurisdiction_code: Mapped[str | None] = mapped_column(
        String(6),
        ForeignKey("jurisdictions.code"),
        comment="Sub-national jurisdiction node (T3 tree), e.g. 'AU-QLD'.",
    )
    postcode: Mapped[str | None] = mapped_column(String(8))
    payment_flags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        comment="Allowed payment systems, e.g. ['DE', 'NPP', 'BPAY']",
    )
