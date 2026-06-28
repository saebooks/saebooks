"""Cross-tenant *principal* model — the MYOB-style accountant / bank identity.

A **Principal** is an identity that may hold scoped grants to *multiple*
tenants at once. It is the opposite shape to a :class:`User`, which lives
inside exactly one tenant (``users.tenant_id``) and is isolated by the
``tenant_isolation`` RLS policy.

Why a separate identity, not a multi-tenant ``User``
---------------------------------------------------
``users`` is FORCE-RLS'd on ``tenant_id``; a user row is, by construction,
owned by one tenant and invisible to every other. That is exactly what we
want for a normal operator and we must not weaken it. An accountant who
services four of Richard's entities (and, at scale, a bank servicing many
customer-tenants) is a fundamentally cross-tenant actor. Rather than punch
a hole in ``users`` isolation, we model the cross-tenant actor as a
first-class, *global* identity (``principals``) that is **never** visible to
a tenant session by default, and grant it access to individual tenants via
an explicit, per-tenant, revocable grant (``principal_tenant_grants``).

The tenant boundary is crossed in exactly ONE place
---------------------------------------------------
The only mechanism that lets a principal operate inside a tenant is the
``act_as_tenant`` service (see ``saebooks.services.principal``): it verifies
an *active grant* and then binds ``app.current_tenant`` to the target tenant
— the identical GUC a native user's request sets. From that point the
principal is subject to the *same* FORCE-RLS policies as a native user of
that tenant; there is no ``BYPASSRLS`` path, no second query engine, no
escape hatch. A principal with no active grant for tenant C can never set
``app.current_tenant = C`` through the service, so RLS returns zero rows for
C exactly as it would for a stranger.

Tables in this module
---------------------
* ``principals`` — global identity. Optional ``owned_tenant_id`` links a
  principal to *its own* books (the accountant's / bank's own ledger).
* ``principal_fido2_credentials`` — the FIDO2/WebAuthn binding. A principal
  authenticates FIDO2-only (standing security rule: no code-based 2FA). We
  model the credential rows + the ``requires_fido2`` invariant here; live
  WebAuthn enrolment is a documented seam (see module
  ``saebooks.services.principal`` and ``docs/security/accountant-principal.md``).
* ``principal_tenant_grants`` — the cross-tenant grant table. **This is the
  security-critical table**; its access rules are described in detail in its
  class docstring and enforced by migration 0155.

None of these three tables are ``CompanyScoped`` — they are not company- or
tenant-filtered by the ORM scope guard, because a principal is not owned by a
company. ``principal_tenant_grants`` *does* carry a ``tenant_id`` and *is*
RLS'd, but with a deliberately bespoke policy (see migration 0155 and the
class docstring) — it is the one table a principal may read across tenants,
and only its own rows.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class PrincipalKind(enum.StrEnum):
    """What sort of cross-tenant actor this principal is.

    ``accountant`` — a bookkeeper/accountant servicing several client
    tenants (Richard's own multi-entity case).

    ``bank`` — same shape at scale: many customer tenants plus the bank's
    own books they reconcile to. The engine models it identically; only the
    frontend differs.
    """

    ACCOUNTANT = "accountant"
    BANK = "bank"


class GrantStatus(enum.StrEnum):
    """Lifecycle of a single tenant→principal grant.

    A grant is ``active`` from creation until a tenant admin revokes it, at
    which point it becomes ``revoked`` and the principal immediately loses
    the ability to act as that tenant. We keep revoked rows (soft-delete)
    for the audit trail rather than hard-deleting them.
    """

    ACTIVE = "active"
    REVOKED = "revoked"


class Principal(Base):
    """A cross-tenant identity (accountant or bank).

    Global, NOT tenant-scoped. A principal is invisible to ordinary tenant
    sessions; only the principal-auth path and the SECURITY DEFINER grant
    resolver ever read this table on behalf of an authenticated principal.
    """

    __tablename__ = "principals"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PrincipalKind.ACCOUNTANT.value,
        server_default=PrincipalKind.ACCOUNTANT.value,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Login identifier — the principal authenticates against this (then a
    # FIDO2 assertion). Unique across all principals.
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A principal MAY own its own books — a tenant it fully owns (its own
    # ledger). Nullable; SET NULL on tenant delete so the principal survives.
    owned_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    # FIDO2-only invariant. Defaults TRUE and there is no setting that turns
    # it off via the API — a principal MUST authenticate with a hardware
    # security key. The column exists so the auth service can assert it and
    # so a future migration could, in principle, distinguish a legacy
    # principal; it must never be flipped false for a real cross-tenant
    # actor. See docs/security/accountant-principal.md.
    requires_fido2: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default=sa.text("true")
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class PrincipalFido2Credential(Base):
    """FIDO2/WebAuthn credential bound to a :class:`Principal`.

    Mirrors ``user_webauthn_credentials`` (migration 0135) but keyed on
    ``principal_id`` and global (not tenant-scoped) — a principal is not
    owned by a tenant. The presence of at least one *active* row here is
    what satisfies the ``requires_fido2`` invariant at login; the auth
    service refuses to issue a principal session if none exists.

    We model the schema and the invariant; we do NOT implement live WebAuthn
    ceremony in this branch. The enrolment seam is documented in
    ``saebooks.services.principal.enrol_fido2_credential`` and the security
    doc.
    """

    __tablename__ = "principal_fido2_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    # WebAuthn credential id (binary), globally unique.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # COSE-encoded public key (binary).
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, server_default="0"
    )
    transports: Mapped[list[str]] = mapped_column(
        # No PG-array server_default in the model: ARRAY[]::varchar[] does not
        # narrow to SQLite and breaks the offline Cashbook schema bootstrap
        # (create_all renders the literal verbatim -> sqlite syntax error).
        # PG keeps its DB-level default via migration 0156; default=list sets
        # [] on insert cross-dialect (mirrors user_webauthn_credential).
        ARRAY(String(16)),
        nullable=False,
        default=list,
    )
    friendly_name: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Security key"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "credential_id", name="uq_principal_fido2_credential_id"
        ),
    )


class PrincipalTenantGrant(Base):
    """A single tenant's grant of a scoped role to a principal.

    **This is the crux table — read its access rules carefully.**

    Shape
    -----
    ``(principal_id, tenant_id, role, status, granted_at, granted_by_user_id,
    revoked_at)``. Each row says: *"tenant ``tenant_id`` grants principal
    ``principal_id`` the scoped role ``role``."* A principal with rows for
    tenants {A, B} can act as A and B; a principal with no row for C can
    never act as C.

    Two readers, two completely different access rules
    --------------------------------------------------
    This table is read from two directions, and the security of the whole
    feature rests on keeping them separate:

    1. **A tenant session** (``app.current_tenant`` set to tenant X). It may
       see and manage only the grants *for tenant X* — "who can act as my
       books." This is enforced by the ordinary ``tenant_isolation`` RLS
       policy (``USING tenant_id = current_setting('app.current_tenant')``),
       identical in shape to every other tenant-scoped table. Crucially, the
       ``WITH CHECK`` half means a tenant admin can only INSERT/UPDATE a
       grant whose ``tenant_id`` equals their own tenant — **a tenant cannot
       forge a grant binding a principal to a tenant that did not grant it.**

    2. **A principal session** wanting to know "which tenants can I act as?"
       must read its own rows *across* tenants — precisely the read that the
       tenant-scoped policy forbids. We do NOT relax the policy to allow
       this. Instead a ``SECURITY DEFINER`` function
       ``principal_visible_grants(p_principal_id)`` (migration 0155) returns
       only ``status='active'`` rows for the *one* principal id passed in.
       The service layer passes the **authenticated** principal's id (from
       the principal's verified session), never a value the caller can
       choose for another principal. This is the same controlled-bypass
       pattern that ``webauthn_lookup_credential`` (0135) uses for the
       discoverable-credential login read.

    Why this is safe
    ----------------
    * The tenant-scoped policy still fully isolates the table for ordinary
      tenant traffic — a tenant admin querying ``principal_tenant_grants``
      sees only their own tenant's grants.
    * The SECURITY DEFINER function is the ONLY cross-tenant read, it is
      parameterised by a single principal id, it filters to that id, and the
      id is supplied by the server from the authenticated session — not by
      the client. A principal cannot enumerate another principal's grants.
    * Acting-as still flows through ``app.current_tenant`` + FORCE-RLS, so
      even after the principal learns it has a grant to tenant A, every data
      read/write it performs in A is bound by A's RLS exactly as a native
      user. The grant table tells the principal *where it may go*; it does
      not itself grant data access.
    """

    __tablename__ = "principal_tenant_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The granting tenant. NOT NULL + FK — satisfies the new-table RLS
    # checklist; the tenant_isolation policy keys on this column.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The scoped role the principal operates under inside this tenant. Reuses
    # the UserRole vocabulary (owner/admin/accountant/bookkeeper/viewer) so
    # downstream role checks are identical to a native user. The grant is the
    # ceiling: the principal can never exceed the granted role in this tenant.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=GrantStatus.ACTIVE.value,
        server_default=GrantStatus.ACTIVE.value,
    )
    # The granting tenant's user who created the grant (audit). Nullable so
    # an admin/CLI-created grant doesn't require a user row.
    granted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One live grant per (principal, tenant). A revoked row plus a fresh
        # active row is allowed because the partial unique index only covers
        # active rows (created in migration 0155).
        UniqueConstraint(
            "principal_id",
            "tenant_id",
            "status",
            name="uq_principal_tenant_grant_principal_tenant_status",
        ),
    )
