"""ATO SBR Machine Credential config (Batch II.5).

One row per ``Company`` that has onboarded — or is mid-onboarding —
a RAM Machine Credential + Software Service ID (SSID) so SAE Books
can lodge STP / BAS payloads via ATO SBR.

Secrets (``keystore_encrypted`` + ``keystore_password_encrypted``)
are Fernet ciphertext produced by ``saebooks.services.crypto``.
Extracted certificate metadata (issuer, subject CN, serial, validity
window) is stored in plaintext so the wizard can render a status card
without needing to decrypt on every page load.

Onboarding progress is tracked via five "confirmed_at" / "verified_at"
timestamps — NULL means "step not yet complete". The wizard walks
the admin through them in order.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class AtoSbrConfig(CompanyScoped, Base):
    """Per-company ATO SBR Machine Credential configuration."""

    __tablename__ = "ato_sbr_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # "self_lodger" | "dsp". Controls whether we need the DSP
    # operational-framework attestations; see the ato-sbr-onboarding
    # memory for the full distinction.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="self_lodger"
    )
    # "evte" | "production". Resolver maps this to the base URL; the
    # credential itself is the same for either environment.
    environment: Mapped[str] = mapped_column(
        String(16), nullable=False, default="evte"
    )

    # Ciphertext — Fernet tokens are variable-length base64, so Text.
    keystore_encrypted: Mapped[str | None] = mapped_column(Text)
    keystore_password_encrypted: Mapped[str | None] = mapped_column(Text)
    keystore_filename: Mapped[str | None] = mapped_column(String(255))
    keystore_subject_cn: Mapped[str | None] = mapped_column(String(255))
    keystore_issuer_cn: Mapped[str | None] = mapped_column(String(255))
    keystore_serial: Mapped[str | None] = mapped_column(String(128))
    keystore_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    keystore_not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    ssid: Mapped[str | None] = mapped_column(String(64))

    # Wizard-step checkpoints. NULL = not yet confirmed.
    mygovid_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    ram_authority_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    downloader_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    evte_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    prod_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
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
