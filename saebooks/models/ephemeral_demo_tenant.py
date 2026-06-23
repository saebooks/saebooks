"""Control table for the public-preview ephemeral demo tenants.

Each public-preview visit mints a *fresh saebooks company* (its own RLS
tenant) so one visitor's scratch data never leaks to the next, and a
background reaper hard-deletes the company when it goes idle or aged out.
This table is the reaper's worklist: one row per live ephemeral demo
company.

DELIBERATE RLS EXEMPTION — read before "fixing"
-----------------------------------------------
This table is **intentionally NOT tenant-scoped** and is therefore
**exempt from the new-tenant-table RLS checklist** (tenant_id NOT NULL +
FK, FORCE RLS, tenant_isolation policy). That is by design, not an
oversight:

* It is a **control-plane** table, not customer ledger data. It holds no
  financial rows — only ``(company_id, timestamps, source_ip,
  request_count)`` bookkeeping the reaper needs.
* The **reaper queries it across all tenants** in one sweep to find idle
  / aged demos. A ``tenant_isolation`` policy keyed on
  ``app.current_tenant`` would hide every row from the cross-tenant
  sweep (which runs with no tenant GUC set), defeating the table's whole
  purpose.
* The data it *points at* — the demo companies' rows — remain fully
  RLS-isolated as ordinary tenants. Isolation between two live demos is
  enforced exactly as for any real tenant (distinct ``tenant_id`` +
  FORCE RLS). This table does not weaken that; it sits beside it.
* It carries no ``tenant_id`` column at all, so there is nothing to
  isolate. ``company_id`` is the PK and FKs to ``companies`` with
  ``ON DELETE CASCADE`` so the reaper's hard-delete of the company drops
  this row automatically.

Membership of this table is the *gate* on the reaper's hard-delete: a
company is only ever hard-deleted if it has a row here. A real company
(no row) can never be touched by the reaper. See
``saebooks.services.ephemeral_demo``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class EphemeralDemoTenant(Base):
    """One row per live public-preview demo company (control-plane only).

    NOT tenant-scoped — see the module docstring for the deliberate RLS
    exemption rationale. The reaper sweeps this table across every
    tenant.
    """

    __tablename__ = "ephemeral_demo_tenants"

    # PK *and* FK to companies. ON DELETE CASCADE so a hard-delete of the
    # demo company removes this control row in the same statement.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Touched on every authenticated request carrying this demo's session,
    # so the reaper can measure idle time. Initialised to created_at.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Provisioning client IP — used for the per-IP provision rate-limit and
    # light abuse forensics. Nullable: the web container may not always
    # forward a trustworthy client IP. Postgres INET; renders as String on
    # the SQLite cashbook backend via db_types compile hooks.
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
