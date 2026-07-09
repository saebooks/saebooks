"""APCA BSB directory — bank/branch lookup for AU bank accounts."""
import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class BsbDirectoryEntry(ReferenceBase):
    __tablename__ = "bsb_directory"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bsb: Mapped[str] = mapped_column(String(6), nullable=False, unique=True)
    bank_name: Mapped[str] = mapped_column(String(128), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(256))
    suburb: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(8))
    postcode: Mapped[str | None] = mapped_column(String(8))
    payment_flags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        comment="Allowed payment systems, e.g. ['DE', 'NPP', 'BPAY']",
    )
