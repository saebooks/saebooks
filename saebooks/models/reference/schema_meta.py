"""Single-row metadata table tagging the seed-set version.

Lets a deployment answer 'which release of the rate seeds is loaded?'
without scraping migration history. Updated by the seed loader on every
``reference-load`` run.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class ReferenceSchemaMeta(ReferenceBase):
    __tablename__ = "schema_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version_tag: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Free-text tag, e.g. '2026-05-09-au-base'",
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
