import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.journal_template import JournalTemplate


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[JournalTemplate]:
    stmt = (
        select(JournalTemplate)
        .where(JournalTemplate.company_id == company_id, JournalTemplate.archived_at.is_(None))
    )
    if tenant_id is not None:
        stmt = stmt.where(JournalTemplate.tenant_id == tenant_id)
    stmt = stmt.order_by(JournalTemplate.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, template_id: uuid.UUID) -> JournalTemplate | None:
    return await session.get(JournalTemplate, template_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str,
    description: str | None = None,
    lines: list[dict[str, Any]],
) -> JournalTemplate:
    tmpl = JournalTemplate(
        company_id=company_id,
        name=name.strip(),
        description=description,
        lines=lines,
    )
    session.add(tmpl)
    await session.commit()
    await session.refresh(tmpl)
    return tmpl


async def create_from_entry(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str,
    description: str | None,
    entry_lines: list[dict[str, Any]],
) -> JournalTemplate:
    """Create a template from an existing journal entry's lines."""
    lines = []
    for line in entry_lines:
        lines.append({
            "account_id": str(line.get("account_id", "")),
            "description": line.get("description", ""),
            "debit": str(line.get("debit", "0")),
            "credit": str(line.get("credit", "0")),
            "tax_code_id": str(line.get("tax_code_id", "")) if line.get("tax_code_id") else "",
        })
    return await create(session, company_id, name=name, description=description, lines=lines)


async def update(
    session: AsyncSession,
    template_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    lines: list[dict[str, Any]] | None = None,
) -> JournalTemplate:
    tmpl = await session.get(JournalTemplate, template_id)
    if tmpl is None:
        raise ValueError(f"Template {template_id} not found")
    if name is not None:
        tmpl.name = name.strip()
    if description is not None:
        tmpl.description = description or None
    if lines is not None:
        tmpl.lines = lines
    await session.commit()
    await session.refresh(tmpl)
    return tmpl


async def archive(session: AsyncSession, template_id: uuid.UUID) -> None:
    tmpl = await session.get(JournalTemplate, template_id)
    if tmpl is None:
        return
    tmpl.archived_at = datetime.now(UTC)
    await session.commit()


async def delete(session: AsyncSession, template_id: uuid.UUID) -> None:
    tmpl = await session.get(JournalTemplate, template_id)
    if tmpl is None:
        return
    await session.delete(tmpl)
    await session.commit()
