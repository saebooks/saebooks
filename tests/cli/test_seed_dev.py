"""Tests for saebooks.cli.seed_dev.

All tests use monkeypatching to avoid hitting the real database.
The goal is to verify the seed logic (skip-if-exists, correct fields,
correct UUIDs) without requiring a live Postgres instance.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from saebooks.cli import seed_dev as mod
from saebooks.models.user import UserRole

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(*, name: str = "My Company") -> MagicMock:
    c = MagicMock()
    c.id = _TENANT_ID
    c.name = name
    return c


def _make_user(*, email: str = "admin@example.com") -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = email
    u.role = UserRole.ADMIN.value
    return u


def _scalars_returning(obj: Any) -> MagicMock:
    """Build a chain mock that returns ``obj`` from ``.scalars().first()``."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = obj
    result.scalars.return_value = scalars
    return result


# ---------------------------------------------------------------------------
# Company seed tests
# ---------------------------------------------------------------------------


async def test_seed_company_creates_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_company calls ensure_seed_company and returns created=True."""
    company = _make_company()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_returning(None))

    fake_ensure = AsyncMock(return_value=company)
    monkeypatch.setattr(mod, "ensure_seed_company", fake_ensure)

    result, created = await mod.seed_company(session)
    assert created is True
    assert result is company
    fake_ensure.assert_awaited_once_with(session)


async def test_seed_company_skips_when_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_company returns created=False when company UUID already in DB."""
    company = _make_company()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_returning(company))

    fake_ensure = AsyncMock()
    monkeypatch.setattr(mod, "ensure_seed_company", fake_ensure)

    result, created = await mod.seed_company(session)
    assert created is False
    assert result is company
    fake_ensure.assert_not_awaited()


async def test_seed_company_uses_default_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_company queries for the well-known default company UUID."""
    captured_stmts: list[Any] = []

    class _FakeResult:
        def scalars(self) -> MagicMock:
            s = MagicMock()
            s.first.return_value = None
            return s

    async def _fake_execute(stmt: Any, *a: Any, **kw: Any) -> _FakeResult:
        captured_stmts.append(stmt)
        return _FakeResult()

    session = AsyncMock()
    session.execute = _fake_execute

    company = _make_company()
    monkeypatch.setattr(mod, "ensure_seed_company", AsyncMock(return_value=company))

    await mod.seed_company(session)

    # The WHERE clause must reference the default tenant UUID via bind params.
    assert captured_stmts, "No execute calls were made"
    stmt = captured_stmts[0]
    # Compile with literal binds so the UUID appears in the rendered SQL.
    # SQLAlchemy renders UUIDs without hyphens on some dialects, so check
    # for both the hyphenated and un-hyphenated forms.
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    compiled_str = str(compiled)
    uuid_no_hyphens = str(_TENANT_ID).replace("-", "")
    assert str(_TENANT_ID) in compiled_str or uuid_no_hyphens in compiled_str, (
        f"Expected {_TENANT_ID} in compiled query, got: {compiled_str}"
    )


# ---------------------------------------------------------------------------
# Admin user seed tests
# ---------------------------------------------------------------------------


async def test_seed_admin_user_creates_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_admin_user inserts a User with ADMIN role when none exists."""
    added: list[Any] = []

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_returning(None))
    session.add = MagicMock(side_effect=added.append)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    monkeypatch.setenv("SAEBOOKS_DEV_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SAEBOOKS_DEV_ADMIN_PASSWORD", "changeme")

    _user, created = await mod.seed_admin_user(session)
    assert created is True
    assert len(added) == 1
    new_user = added[0]
    assert new_user.email == "admin@example.com"
    assert new_user.role == UserRole.ADMIN.value
    assert new_user.password_hash is not None
    assert new_user.password_hash.startswith("pbkdf2sha256$")


async def test_seed_admin_user_skips_when_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_admin_user returns created=False when email already in DB."""
    existing_user = _make_user()

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_returning(existing_user))
    session.add = MagicMock()

    monkeypatch.setenv("SAEBOOKS_DEV_ADMIN_EMAIL", "admin@example.com")

    user, created = await mod.seed_admin_user(session)
    assert created is False
    assert user is existing_user
    session.add.assert_not_called()


async def test_seed_admin_user_password_is_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored password hash verifies against the plain-text password."""
    from saebooks.services.jwt_tokens import verify_password

    added: list[Any] = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalars_returning(None))
    session.add = MagicMock(side_effect=added.append)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    monkeypatch.setenv("SAEBOOKS_DEV_ADMIN_EMAIL", "dev@example.com")
    monkeypatch.setenv("SAEBOOKS_DEV_ADMIN_PASSWORD", "s3cr3t!")

    await mod.seed_admin_user(session)
    new_user = added[0]
    assert verify_password("s3cr3t!", new_user.password_hash)
    assert not verify_password("wrong", new_user.password_hash)


# ---------------------------------------------------------------------------
# Tax code seed tests
# ---------------------------------------------------------------------------


async def test_seed_tax_codes_calls_ensure_au_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_tax_codes delegates to ensure_au_seed and returns count."""
    company = _make_company()
    session = AsyncMock()

    fake_ensure = AsyncMock(return_value=6)
    monkeypatch.setattr(mod, "ensure_tax_codes", fake_ensure)

    count = await mod.seed_tax_codes(session, company)
    assert count == 6
    fake_ensure.assert_awaited_once_with(session, company.id)


async def test_seed_tax_codes_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_au_seed returns 0 when all codes already present — seed respects that."""
    company = _make_company()
    session = AsyncMock()

    fake_ensure = AsyncMock(return_value=0)
    monkeypatch.setattr(mod, "ensure_tax_codes", fake_ensure)

    count = await mod.seed_tax_codes(session, company)
    assert count == 0


# ---------------------------------------------------------------------------
# CoA range test (via live au seed data, no DB required)
# ---------------------------------------------------------------------------


def test_coa_seed_csv_has_correct_account_ranges() -> None:
    """The AU seed CSV covers the five standard account type ranges."""
    import csv
    from pathlib import Path

    from saebooks.seed.load_au_coa import ODOO_TYPE_MAP, _hyphenate_code

    csv_path = Path(__file__).parent.parent.parent / "saebooks/seed/au/account.account-au.csv"
    assert csv_path.exists(), f"Seed CSV not found at {csv_path}"

    types_seen: set[str] = set()
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            raw_code = row["code"].strip()
            odoo_type = row["account_type"].strip()
            if odoo_type in ODOO_TYPE_MAP:
                code = _hyphenate_code(raw_code)
                prefix = code.split("-")[0]
                types_seen.add(prefix)

    # Expect at least accounts in the 1–5 prefix ranges (assets through expenses).
    for expected_prefix in ("1", "2", "3", "4", "5"):
        assert expected_prefix in types_seen, (
            f"No accounts with prefix {expected_prefix!r} found in AU seed CSV"
        )
