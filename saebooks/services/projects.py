"""Project (job) service — CRUD + search + archive.

Projects are per-company tags that get stamped on journal / invoice /
bill lines for job-costing reports (see `services/reports.py`). This
service is a thin CRUD layer mirroring the Contact service pattern.

`code` is the short human identifier (e.g. ``J-001``) and is unique
within a company. `status` moves ACTIVE → COMPLETED → ARCHIVED —
COMPLETED projects are still visible in reports but excluded from the
line-item picker by default.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.project import Project, ProjectStatus
from saebooks.services import audit as audit_svc


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: ProjectStatus | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Project]:
    """List projects for a company.

    By default returns non-archived, with all statuses. Pass
    ``status=ProjectStatus.ACTIVE`` when rendering the line-item
    picker so completed projects don't clutter the dropdown.
    """
    stmt = select(Project).where(Project.company_id == company_id)
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(Project.status == status)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Project.name.ilike(pattern) | Project.code.ilike(pattern))
    stmt = stmt.order_by(Project.code).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, project_id: uuid.UUID) -> Project | None:
    return await session.get(Project, project_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    name: str,
    status: ProjectStatus = ProjectStatus.ACTIVE,
    start_date: date | None = None,
    end_date: date | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Project:
    """Create a project. Raises on duplicate (company_id, code)."""
    project = Project(
        company_id=company_id,
        code=code.strip(),
        name=name.strip(),
        status=status,
        start_date=start_date,
        end_date=end_date,
        notes=notes,
        extra=extra,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


_ALLOWED_UPDATE_FIELDS = frozenset({
    "code", "name", "status", "start_date", "end_date", "notes", "extra",
})


async def update(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    **kwargs: Any,
) -> Project:
    """Update project fields. Only fields in `_ALLOWED_UPDATE_FIELDS`."""
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    if "code" in kwargs and kwargs["code"] is not None:
        kwargs["code"] = kwargs["code"].strip()
    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()
    if "status" in kwargs and kwargs["status"] is not None:
        # Validate — fail closed on bad input rather than letting the
        # CHECK constraint fire at flush time with a cryptic message.
        raw = kwargs["status"]
        if isinstance(raw, str):
            raw = ProjectStatus(raw)
        kwargs["status"] = raw

    before = audit_svc.capture(project)
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            raise ValueError(f"Unknown field: {key}")
        setattr(project, key, value)

    await audit_svc.snapshot_row(
        session, project,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    await session.refresh(project)
    return project


async def archive(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    """Soft-delete. Also bumps status to ARCHIVED so list filters pick it up."""
    project = await session.get(Project, project_id)
    if project is None:
        return
    before = audit_svc.capture(project)
    project.archived_at = datetime.now(UTC)
    project.status = ProjectStatus.ARCHIVED
    await audit_svc.snapshot_row(
        session, project,
        action="archive",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
