"""Project (job) service — CRUD + search + archive.

Projects are per-company tags that get stamped on journal / invoice /
bill lines for job-costing reports (see `services/reports.py`). This
service is a thin CRUD layer mirroring the Contact service pattern.

`code` is the short human identifier (e.g. ``J-001``) and is unique
within a company. `status` moves ACTIVE → COMPLETED → ARCHIVED —
COMPLETED projects are still visible in reports but excluded from the
line-item picker by default.

API-tier functions (``api_create``, ``api_update``, ``api_delete``,
``list_projects``, ``api_get``) added in Phase 1 tier-4 (cycle 13)
to support ``/api/v1/projects`` with optimistic locking + change_log.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.project import Project, ProjectStatus
from saebooks.services import audit as audit_svc
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Columns written to change_log.payload for project operations.
_PROJECT_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "code",
    "name",
    "status",
    "start_date",
    "end_date",
    "notes",
    "extra",
    "version",
    "created_at",
    "archived_at",
)


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    status: ProjectStatus | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> list[Project]:
    """List projects for a company.

    By default returns non-archived, with all statuses. Pass
    ``status=ProjectStatus.ACTIVE`` when rendering the line-item
    picker so completed projects don't clutter the dropdown.

    P0 defence-in-depth: when ``tenant_id`` is supplied, the query
    is additionally filtered by tenant.
    """
    stmt = select(Project).where(Project.company_id == company_id)
    if tenant_id is not None:
        stmt = stmt.where(Project.tenant_id == tenant_id)
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


async def get(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Project | None:
    """Fetch a project by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists, so
    cross-tenant probes 404. Belt-and-braces complement to FORCE RLS.
    """
    if tenant_id is None and company_id is None:
        return await session.get(Project, project_id)
    clauses = [Project.id == project_id]
    if tenant_id is not None:
        clauses.append(Project.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Project.company_id == company_id)
    result = await session.execute(
        select(Project).where(*clauses)
    )
    return result.scalars().first()


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
    tenant_id: uuid.UUID | None = None,
    **kwargs: Any,
) -> Project:
    """Update project fields. Only fields in `_ALLOWED_UPDATE_FIELDS`.

    When ``tenant_id`` is supplied, a foreign-tenant id raises
    ``ValueError`` (treated as not found) — cross-tenant probes 404.
    """
    project = await get(session, project_id, tenant_id=tenant_id)
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
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Soft-delete. Also bumps status to ARCHIVED so list filters pick it up.

    When ``tenant_id`` is supplied, a foreign-tenant id is silently
    treated as "no row" — cross-tenant archive becomes a no-op.
    """
    project = await get(session, project_id, tenant_id=tenant_id)
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


# ---------------------------------------------------------------------------
# API-tier helpers
# ---------------------------------------------------------------------------


def _serialise(project: Project) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _PROJECT_COLUMNS:
        val = getattr(project, key, None)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif hasattr(val, "isoformat"):  # date
            val = val.isoformat()
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# ---------------------------------------------------------------------------
# API-tier exceptions
# ---------------------------------------------------------------------------


class ProjectApiError(ValueError):
    """Raised on project validation or state-transition failure (API tier)."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Project) -> None:
        super().__init__(
            f"Project {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# API-tier read operations
# ---------------------------------------------------------------------------


async def list_projects(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Project], int]:
    """Return (projects, total_count) filtered by status/archived flag."""
    where = [Project.company_id == company_id, Project.tenant_id == tenant_id]

    if not archived:
        where.append(Project.archived_at.is_(None))
    else:
        where.append(Project.archived_at.isnot(None))

    if status is not None:
        where.append(Project.status == status)

    count_stmt = select(sa_func.count()).select_from(Project).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Project)
        .where(*where)
        .order_by(Project.code)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def api_get(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Project | None:
    """Fetch a single project by primary key.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.
    """
    if tenant_id is None and company_id is None:
        return await session.get(Project, project_id)
    clauses = [Project.id == project_id]
    if tenant_id is not None:
        clauses.append(Project.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Project.company_id == company_id)
    result = await session.execute(
        select(Project).where(*clauses)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# API-tier write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    code: str,
    name: str,
    status: str = "ACTIVE",
    start_date: date | None = None,
    end_date: date | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Project:
    """Create a new project row with version=1 and change_log entry."""
    project = Project(
        company_id=company_id,
        tenant_id=tenant_id,
        code=code.strip(),
        name=name.strip(),
        status=ProjectStatus(status),
        start_date=start_date,
        end_date=end_date,
        notes=notes,
        extra=extra,
        version=1,
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)

    await change_log_svc.append(
        session,
        entity="project",
        entity_id=project.id,
        op="created",
        actor=actor,
        payload=_serialise(project),
        version=project.version,
    )
    await session.commit()
    await session.refresh(project)
    return project


async def api_update(
    session: AsyncSession,
    project_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    code: str | None = None,
    name: str | None = None,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Project:
    """Update project fields with optimistic locking + change_log."""
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectApiError(f"Project {project_id} not found")
    if project.version != expected_version:
        raise VersionConflict(project)

    if code is not None:
        project.code = code.strip()
    if name is not None:
        project.name = name.strip()
    if status is not None:
        project.status = ProjectStatus(status)
    if start_date is not None:
        project.start_date = start_date
    if end_date is not None:
        project.end_date = end_date
    if notes is not None:
        project.notes = notes
    if extra is not None:
        project.extra = extra

    project.version = project.version + 1
    await session.flush()
    await session.refresh(project)

    await change_log_svc.append(
        session,
        entity="project",
        entity_id=project.id,
        op="updated",
        actor=actor,
        payload=_serialise(project),
        version=project.version,
    )
    await session.commit()
    await session.refresh(project)
    return project


async def api_delete(
    session: AsyncSession,
    project_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Project:
    """Soft-archive a project with optimistic locking + change_log."""
    project = await session.get(Project, project_id)
    if project is None:
        raise ProjectApiError(f"Project {project_id} not found")
    if project.version != expected_version:
        raise VersionConflict(project)

    project.archived_at = datetime.now(UTC)
    project.version = project.version + 1
    await session.flush()
    await session.refresh(project)

    await change_log_svc.append(
        session,
        entity="project",
        entity_id=project.id,
        op="deleted",
        actor=actor,
        payload=_serialise(project),
        version=project.version,
    )
    await session.commit()
    return project


__all__ = [
    "ProjectApiError",
    "VersionConflict",
    "api_create",
    "api_delete",
    "api_get",
    "api_update",
    "list_projects",
]
