"""Project (job) model for job-costing + P&L-by-project reports.

Projects are per-company tags that can be stamped on journal /
invoice / bill lines. A stamped line then shows up in the P&L-by-
project report (see :mod:`saebooks.services.reports`).

Kept deliberately minimal — name, code, status, optional date range
+ notes + extra JSONB. Sub-projects, billing rates, and project
budgets all land in later batches once a real user needs them.

Soft-delete via ``archived_at`` mirrors the Contact pattern: we never
hard-delete a project because GL history may still reference it. The
``project_id`` FK on line tables is ``ON DELETE SET NULL`` as a
defence-in-depth if someone does manage to hard-delete via SQL.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class ProjectStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class Project(CompanyScoped, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_projects_company_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        String(16),
        nullable=False,
        default=ProjectStatus.ACTIVE,
    )
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"<Project {self.code} {self.name}>"
