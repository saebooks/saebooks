"""Security tests for the cross-tenant *principal* (accountant / bank).

This is the most security-sensitive surface in the system: it deliberately
lets ONE identity cross tenant boundaries. These tests prove the boundary is
crossed ONLY where intended and stays sealed everywhere else.

Every assertion below runs against the ``saebooks_app`` Postgres role
(NOSUPERUSER, NOBYPASSRLS) so FORCE ROW LEVEL SECURITY actually fires — the
same harness ``test_cross_tenant_isolation.py`` uses. A test that passed
under the BYPASSRLS owner role would prove nothing. Seeding uses the owner
role so we can place rows into any tenant deterministically.

What is proven
--------------
1. ``act_as`` binds A and B for a principal granted {A, B}, and reads/writes
   land in the bound tenant only.
2. The principal **cannot** act as C (no grant) — ``NoActiveGrant`` raised,
   GUC never set, zero rows for C.
3. Revoking the grant to A immediately removes A access.
4. The grant table's own visibility rule:
   * a principal sees ONLY its own active grants across tenants (via the
     SECURITY DEFINER resolver);
   * a tenant session sees ONLY its own tenant's grant rows (tenant_isolation);
   * a tenant CANNOT forge a grant for a foreign tenant (WITH CHECK).
5. A non-granted principal resolves zero actable tenants.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

os.environ.setdefault(
    "SAEBOOKS_SECRET_KEY", "test-secret-key-for-principal-tests"
)

# NOTE: deliberately NOT ``saebooks.db.engine`` — that's the runtime
# engine, which IS the saebooks_app role under --rls (see
# docker-compose.test.yml). ``_set_app_role_password`` (ALTER ROLE) and
# the ``owner_sessionmaker`` fixture below both need the real
# owner/superuser role regardless of --rls.
from saebooks.db import _owner_role_engine as _owner_engine
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.principal import (
    GrantStatus,
    Principal,
    PrincipalKind,
    PrincipalTenantGrant,
)
from saebooks.models.tenant import Tenant
from saebooks.services.principal import (
    Fido2NotEnrolled,
    NoActiveGrant,
    assert_fido2_satisfied,
    bind_session_to_tenant,
    enrol_fido2_credential,
    list_actable_tenants,
    resolve_grant_role,
)

# --------------------------------------------------------------------------- #
# saebooks_app (NOBYPASSRLS) engine — same pattern as test_cross_tenant_isolation
# --------------------------------------------------------------------------- #

_APP_ROLE_PASSWORD = "saebooks_app_test_pw"


def _build_app_engine_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    from saebooks.config import settings

    parts = urlsplit(settings.database_url)
    netloc = f"saebooks_app:{_APP_ROLE_PASSWORD}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


async def _set_app_role_password() -> None:
    async with _owner_engine.begin() as conn:
        await conn.execute(
            text(
                f"ALTER ROLE saebooks_app WITH PASSWORD '{_APP_ROLE_PASSWORD}'"
            )
        )


@pytest_asyncio.fixture(scope="module")
async def app_sessionmaker() -> AsyncIterator[Any]:
    """Sessionmaker bound to the NOBYPASSRLS saebooks_app role."""
    await _set_app_role_password()
    eng = create_async_engine(
        _build_app_engine_url(), poolclass=NullPool, future=True
    )
    yield async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    await eng.dispose()


@pytest_asyncio.fixture(scope="module")
async def owner_sessionmaker() -> AsyncIterator[Any]:
    yield async_sessionmaker(
        _owner_engine, expire_on_commit=False, class_=AsyncSession
    )


# --------------------------------------------------------------------------- #
# Seed: three tenants (A, B, C) each with a company + a marker contact, plus
# one principal granted {A, B} and one principal granted nothing.
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(scope="module")
async def seeded(owner_sessionmaker: Any) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    out: dict[str, Any] = {"tenants": {}}

    async with owner_sessionmaker() as s:
        for label in ("A", "B", "C"):
            tid = uuid.uuid4()
            cid = uuid.uuid4()
            contact_id = uuid.uuid4()
            s.add(
                Tenant(id=tid, name=f"P-{label}-{suffix}", slug=f"p-{label}-{suffix}")
            )
            await s.flush()
            s.add(
                Company(
                    id=cid,
                    tenant_id=tid,
                    name=f"P-Co-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await s.flush()
            s.add(
                Contact(
                    id=contact_id,
                    tenant_id=tid,
                    company_id=cid,
                    name=f"Marker-{label}",
                    contact_type=ContactType.CUSTOMER,
                )
            )
            await s.flush()
            out["tenants"][label] = {
                "tenant_id": tid,
                "company_id": cid,
                "contact_id": contact_id,
            }

        # Principal granted {A, B}
        p_ab = Principal(
            id=uuid.uuid4(),
            kind=PrincipalKind.ACCOUNTANT.value,
            display_name="Accountant AB",
            username=f"acct-ab-{suffix}",
        )
        # Principal granted nothing
        p_none = Principal(
            id=uuid.uuid4(),
            kind=PrincipalKind.ACCOUNTANT.value,
            display_name="Accountant None",
            username=f"acct-none-{suffix}",
        )
        s.add_all([p_ab, p_none])
        await s.flush()

        for label in ("A", "B"):
            s.add(
                PrincipalTenantGrant(
                    id=uuid.uuid4(),
                    principal_id=p_ab.id,
                    tenant_id=out["tenants"][label]["tenant_id"],
                    role="accountant",
                    status=GrantStatus.ACTIVE.value,
                )
            )
        await s.commit()

        out["principal_ab"] = p_ab.id
        out["principal_none"] = p_none.id
        out["suffix"] = suffix

    yield out

    # Cleanup (owner role, FK-safe order).
    async with owner_sessionmaker() as s:
        await s.execute(
            text(
                "DELETE FROM principal_tenant_grants "
                "WHERE principal_id IN (:a, :b)"
            ),
            {"a": str(out["principal_ab"]), "b": str(out["principal_none"])},
        )
        await s.execute(
            text("DELETE FROM principals WHERE id IN (:a, :b)"),
            {"a": str(out["principal_ab"]), "b": str(out["principal_none"])},
        )
        for label in ("A", "B", "C"):
            t = out["tenants"][label]
            await s.execute(
                text("DELETE FROM contacts WHERE id = :id"),
                {"id": str(t["contact_id"])},
            )
            await s.execute(
                text("DELETE FROM companies WHERE id = :id"),
                {"id": str(t["company_id"])},
            )
            await s.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": str(t["tenant_id"])},
            )
        await s.commit()


# --------------------------------------------------------------------------- #
# 1. Principal granted {A, B} can act-as A and B; reads land in the bound tenant.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label", ["A", "B"])
async def test_act_as_granted_tenant_reads_only_that_tenant(
    app_sessionmaker: Any, seeded: dict[str, Any], label: str
) -> None:
    """act_as(A) reads A's marker contact and nothing from B or C.

    Run under saebooks_app (NOBYPASSRLS) so the WHERE is enforced by RLS, not
    just by an app filter.
    """
    pid = seeded["principal_ab"]
    target = seeded["tenants"][label]

    async with app_sessionmaker() as s:
        async with s.begin():
            role = await bind_session_to_tenant(s, pid, target["tenant_id"])
            assert role == "accountant"
            rows = (
                await s.execute(text("SELECT id, name, tenant_id FROM contacts"))
            ).all()
        # Every visible row must belong to the bound tenant.
        assert rows, f"expected to see {label}'s contact while acting as {label}"
        for r in rows:
            assert str(r.tenant_id) == str(target["tenant_id"]), (
                f"CROSS-TENANT LEAK: while acting as {label}, saw a contact "
                f"for tenant {r.tenant_id}"
            )
        ids = {str(r.id) for r in rows}
        assert str(target["contact_id"]) in ids


async def test_act_as_can_switch_between_granted_tenants(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """One principal switches A -> B in separate transactions; each isolated."""
    pid = seeded["principal_ab"]
    a = seeded["tenants"]["A"]
    b = seeded["tenants"]["B"]

    async with app_sessionmaker() as s:
        async with s.begin():
            await bind_session_to_tenant(s, pid, a["tenant_id"])
            a_rows = (
                await s.execute(text("SELECT tenant_id FROM contacts"))
            ).all()
        assert all(str(r.tenant_id) == str(a["tenant_id"]) for r in a_rows)

        async with s.begin():
            await bind_session_to_tenant(s, pid, b["tenant_id"])
            b_rows = (
                await s.execute(text("SELECT tenant_id FROM contacts"))
            ).all()
        assert all(str(r.tenant_id) == str(b["tenant_id"]) for r in b_rows)


# --------------------------------------------------------------------------- #
# 2. Principal granted {A, B} CANNOT act-as C (no grant). Proven under RLS.
# --------------------------------------------------------------------------- #


async def test_act_as_non_granted_tenant_denied(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """act_as(C) raises NoActiveGrant — and the GUC is never set."""
    pid = seeded["principal_ab"]
    c = seeded["tenants"]["C"]

    async with app_sessionmaker() as s, s.begin():
        with pytest.raises(NoActiveGrant):
            await bind_session_to_tenant(s, pid, c["tenant_id"])
        # GUC must NOT have been set — RLS gives zero rows for C.
        rows = (
            await s.execute(text("SELECT id FROM contacts"))
        ).all()
        assert rows == [], (
            "after a denied act_as, the session must see zero rows — a "
            "non-empty result means the GUC leaked or RLS is off"
        )


async def test_resolve_grant_role_none_for_non_granted(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """The grant predicate returns None for a tenant the principal lacks."""
    pid = seeded["principal_ab"]
    c = seeded["tenants"]["C"]
    async with app_sessionmaker() as s, s.begin():
        assert await resolve_grant_role(s, pid, c["tenant_id"]) is None
        assert (
            await resolve_grant_role(
                s, pid, seeded["tenants"]["A"]["tenant_id"]
            )
            == "accountant"
        )


# --------------------------------------------------------------------------- #
# 3. Revoke -> access gone immediately.
# --------------------------------------------------------------------------- #


async def test_revoke_grant_removes_access(
    app_sessionmaker: Any, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Revoking the grant to A makes act_as(A) raise, and zero rows for A.

    Uses a throwaway principal + grant so the module-scoped seed stays intact.
    """
    a = seeded["tenants"]["A"]
    suffix = seeded["suffix"]

    # Fresh principal granted only A.
    async with owner_sessionmaker() as s:
        p = Principal(
            id=uuid.uuid4(),
            kind=PrincipalKind.ACCOUNTANT.value,
            display_name="Revoke Test",
            username=f"acct-revoke-{suffix}",
        )
        s.add(p)
        await s.flush()
        grant = PrincipalTenantGrant(
            id=uuid.uuid4(),
            principal_id=p.id,
            tenant_id=a["tenant_id"],
            role="accountant",
            status=GrantStatus.ACTIVE.value,
        )
        s.add(grant)
        await s.commit()
        pid = p.id
        grant_id = grant.id

    try:
        # Before revoke: can act as A.
        async with app_sessionmaker() as s, s.begin():
            assert (
                await resolve_grant_role(s, pid, a["tenant_id"])
                == "accountant"
            )

        # Revoke (a tenant-A admin would do this; we use owner for the test).
        async with owner_sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE principal_tenant_grants "
                    "SET status='revoked', revoked_at=now() WHERE id=:id"
                ),
                {"id": str(grant_id)},
            )
            await s.commit()

        # After revoke: cannot act as A, zero rows.
        async with app_sessionmaker() as s, s.begin():
            assert await resolve_grant_role(s, pid, a["tenant_id"]) is None
            with pytest.raises(NoActiveGrant):
                await bind_session_to_tenant(s, pid, a["tenant_id"])
            rows = (await s.execute(text("SELECT id FROM contacts"))).all()
            assert rows == []
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(
                text("DELETE FROM principal_tenant_grants WHERE id=:id"),
                {"id": str(grant_id)},
            )
            await s.execute(
                text("DELETE FROM principals WHERE id=:id"), {"id": str(pid)}
            )
            await s.commit()


# --------------------------------------------------------------------------- #
# 4. The grant table's own visibility rules.
# --------------------------------------------------------------------------- #


async def test_principal_sees_only_own_active_grants(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """list_actable_tenants returns exactly {A, B} for the AB principal."""
    pid = seeded["principal_ab"]
    async with app_sessionmaker() as s, s.begin():
        actable = await list_actable_tenants(s, pid)
    seen = {str(t.tenant_id) for t in actable}
    expected = {
        str(seeded["tenants"]["A"]["tenant_id"]),
        str(seeded["tenants"]["B"]["tenant_id"]),
    }
    assert seen == expected, f"expected {{A,B}}, got {seen}"
    assert all(t.role == "accountant" for t in actable)


async def test_non_granted_principal_resolves_zero_tenants(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """A principal with no grants sees zero actable tenants and is denied all."""
    pid = seeded["principal_none"]
    async with app_sessionmaker() as s, s.begin():
        actable = await list_actable_tenants(s, pid)
        assert actable == []
        for label in ("A", "B", "C"):
            with pytest.raises(NoActiveGrant):
                await bind_session_to_tenant(
                    s, pid, seeded["tenants"][label]["tenant_id"]
                )


async def test_tenant_session_sees_only_its_own_grants(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Under app.current_tenant=A, a direct SELECT on the grant table shows
    only A's grant rows — tenant_isolation enforced by RLS for the
    tenant-facing read direction.
    """
    a = seeded["tenants"]["A"]
    b = seeded["tenants"]["B"]
    async with app_sessionmaker() as s, s.begin():
        await s.execute(
            text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
        )
        rows = (
            await s.execute(
                text("SELECT tenant_id FROM principal_tenant_grants")
            )
        ).all()
    tids = {str(r.tenant_id) for r in rows}
    assert tids == {str(a["tenant_id"])}, (
        f"tenant A's session must see only A's grants; saw {tids}"
    )
    assert str(b["tenant_id"]) not in tids


async def test_tenant_cannot_forge_grant_for_foreign_tenant(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """A tenant-A session cannot INSERT a grant whose tenant_id is B.

    The tenant_isolation WITH CHECK rejects it — a tenant cannot bind a
    principal to a tenant that did not grant it.
    """
    a = seeded["tenants"]["A"]
    b = seeded["tenants"]["B"]
    pid = seeded["principal_none"]

    async with app_sessionmaker() as s:
        with pytest.raises(Exception) as exc:
            async with s.begin():
                await s.execute(
                    text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
                )
                await s.execute(
                    text(
                        "INSERT INTO principal_tenant_grants "
                        "(id, principal_id, tenant_id, role, status) "
                        "VALUES (gen_random_uuid(), :pid, :tid, 'accountant', "
                        "'active')"
                    ),
                    {"pid": str(pid), "tid": str(b["tenant_id"])},
                )
        # Postgres raises a row-level-security WITH CHECK violation.
        assert "row-level security" in str(exc.value).lower() or (
            "policy" in str(exc.value).lower()
        )


async def test_tenant_can_grant_for_its_own_tenant(
    app_sessionmaker: Any, owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """Positive control: a tenant-A session CAN insert a grant for tenant A.

    Proves the WITH CHECK rejection above is specific to the foreign-tenant
    case, not a blanket insert failure.
    """
    a = seeded["tenants"]["A"]
    suffix = seeded["suffix"]
    async with owner_sessionmaker() as s:
        p = Principal(
            id=uuid.uuid4(),
            kind=PrincipalKind.ACCOUNTANT.value,
            display_name="Self Grant",
            username=f"acct-self-{suffix}",
        )
        s.add(p)
        await s.commit()
        pid = p.id

    new_grant_id = uuid.uuid4()
    try:
        async with app_sessionmaker() as s, s.begin():
            await s.execute(
                text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
            )
            await s.execute(
                text(
                    "INSERT INTO principal_tenant_grants "
                    "(id, principal_id, tenant_id, role, status) "
                    "VALUES (:id, :pid, :tid, 'accountant', 'active')"
                ),
                {
                    "id": str(new_grant_id),
                    "pid": str(pid),
                    "tid": str(a["tenant_id"]),
                },
            )
        # Now the principal can act as A.
        async with app_sessionmaker() as s, s.begin():
            assert (
                await resolve_grant_role(s, pid, a["tenant_id"])
                == "accountant"
            )
    finally:
        async with owner_sessionmaker() as s:
            await s.execute(
                text("DELETE FROM principal_tenant_grants WHERE id=:id"),
                {"id": str(new_grant_id)},
            )
            await s.execute(
                text("DELETE FROM principals WHERE id=:id"), {"id": str(pid)}
            )
            await s.commit()


async def test_invalid_role_rejected_by_coherence_trigger(
    app_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """The role coherence trigger fails closed on an unknown role string."""
    a = seeded["tenants"]["A"]
    pid = seeded["principal_none"]
    async with app_sessionmaker() as s:
        with pytest.raises(Exception) as exc:
            async with s.begin():
                await s.execute(
                    text(f"SET LOCAL app.current_tenant = '{a['tenant_id']}'")
                )
                await s.execute(
                    text(
                        "INSERT INTO principal_tenant_grants "
                        "(id, principal_id, tenant_id, role, status) "
                        "VALUES (gen_random_uuid(), :pid, :tid, 'superuser', "
                        "'active')"
                    ),
                    {"pid": str(pid), "tid": str(a["tenant_id"])},
                )
        assert "valid scoped role" in str(exc.value).lower() or (
            "superuser" in str(exc.value).lower()
        )


# --------------------------------------------------------------------------- #
# 5. FIDO2-only auth seam.
# --------------------------------------------------------------------------- #


async def test_fido2_required_blocks_session_without_credential(
    owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """A principal that requires FIDO2 but has none raises Fido2NotEnrolled."""
    async with owner_sessionmaker() as s:
        p = await s.get(Principal, seeded["principal_ab"])
        assert p is not None
        assert p.requires_fido2 is True
        with pytest.raises(Fido2NotEnrolled):
            await assert_fido2_satisfied(s, p)


async def test_fido2_enrolment_then_satisfied(
    owner_sessionmaker: Any, seeded: dict[str, Any]
) -> None:
    """After a credential is enrolled, the FIDO2 gate is satisfied.

    Exercises the persistence half of the enrolment seam (the WebAuthn
    ceremony itself is deferred — see services.principal.enrol_fido2_credential).
    """
    suffix = seeded["suffix"]
    async with owner_sessionmaker() as s:
        p = Principal(
            id=uuid.uuid4(),
            kind=PrincipalKind.ACCOUNTANT.value,
            display_name="Fido Test",
            username=f"acct-fido-{suffix}",
        )
        s.add(p)
        await s.flush()
        # No credential yet -> blocked.
        with pytest.raises(Fido2NotEnrolled):
            await assert_fido2_satisfied(s, p)
        # Enrol -> satisfied.
        await enrol_fido2_credential(
            s,
            p.id,
            credential_id=b"\x01\x02\x03" + suffix.encode(),
            public_key=b"\xaa\xbb\xcc",
            transports=["usb", "nfc"],
            friendly_name="Test YubiKey",
        )
        await assert_fido2_satisfied(s, p)
        await s.rollback()
