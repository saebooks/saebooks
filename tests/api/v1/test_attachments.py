"""Phase 1 contract tests for /api/v1/attachments.

The vault is mocked at the ``saebooks.services.vault`` boundary —
these tests do NOT speak HTTP to a real vault container. The unit
contract is the saebooks-side router behaviour:

* Auth gate (401 without bearer).
* Vault disabled (503) when ``settings.vault_enabled`` is false.
* Upload happy path: entity-existence check → vault upload → vault link
  → 201 with normalised shape.
* List filters by entity, normalises shape.
* Download proxies bytes from the vault stream.
* Delete returns 204.
* Cross-tenant entity_id rejected with 403 (without leaking existence).
* Unknown entity_kind rejected with 422.
* Vault unavailable (5xx / connect error) propagates as 502.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.config import settings as _settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.contact import Contact
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module runs with the vault flipped on
    and a non-empty shared secret. Individual tests can flip it off
    with another monkeypatch call to exercise the 503 path.
    """
    monkeypatch.setattr(_settings, "vault_enabled", True)
    monkeypatch.setattr(_settings, "vault_shared_secret", "test-secret")


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def seed_bill() -> uuid.UUID:
    """Create a fresh Bill in the default tenant and return its UUID.

    We need an entity that lives in the saebooks DB so the existence
    check passes. Reuse the seeded contact + an EXPENSE account so we
    don't have to mint our own.
    """
    async with AsyncSessionLocal() as session:
        account = (
            await session.execute(
                select(Account)
                .where(
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                )
                .limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
        assert account is not None and contact is not None

        # company_id is required by the CompanyScoped mixin — pull from
        # the contact (same tenant by definition, same company in the
        # seed setup).
        from datetime import date as _date
        from decimal import Decimal

        bill = Bill(
            tenant_id=DEFAULT_TENANT_ID,
            company_id=contact.company_id,
            contact_id=contact.id,
            issue_date=_date(2026, 4, 1),
            due_date=_date(2026, 5, 1),
            status=BillStatus.DRAFT.value,
            subtotal=Decimal("0"),
            tax_total=Decimal("0"),
            total=Decimal("0"),
            amount_paid=Decimal("0"),
            currency="AUD",
            fx_rate=Decimal("1"),
            base_subtotal=Decimal("0"),
            base_tax_total=Decimal("0"),
            base_total=Decimal("0"),
            base_amount_paid=Decimal("0"),
        )
        session.add(bill)
        await session.commit()
        await session.refresh(bill)
        return bill.id


@pytest.fixture
async def seed_contact() -> uuid.UUID:
    """Return any contact id from the default tenant."""
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
        assert contact is not None
        return contact.id


# ---------------------------------------------------------------------------
# Vault stub helpers — patched into ``saebooks.services.vault`` per test.
# ---------------------------------------------------------------------------


def _file_meta(file_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(file_id),
        "tenant_id": str(DEFAULT_TENANT_ID),
        "filename": "receipt.pdf",
        "mime": "application/pdf",
        "size_bytes": 4,
        "sha256": "abcd",
        "uploaded_by": "saebooks:api-token",
        "uploaded_at": "2026-05-08T01:00:00Z",
        "preview_state": "pending",
        "archived_at": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth + enablement gates
# ---------------------------------------------------------------------------


async def test_attachments_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get(
        "/api/v1/attachments",
        params={"entity_kind": "bill", "entity_id": str(uuid.uuid4())},
    )
    assert r.status_code == 401


async def test_attachments_disabled_returns_503(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    seed_bill: uuid.UUID,
) -> None:
    monkeypatch.setattr(_settings, "vault_enabled", False)
    r = await api_client.get(
        "/api/v1/attachments",
        params={"entity_kind": "bill", "entity_id": str(seed_bill)},
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def test_upload_happy_path(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    seed_bill: uuid.UUID,
) -> None:
    file_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    async def fake_upload(tenant_id, *, file, filename, content_type, actor):
        captured["upload"] = {
            "tenant_id": tenant_id,
            "filename": filename,
            "content_type": content_type,
            "actor": actor,
            "size": len(file) if isinstance(file, (bytes, bytearray)) else None,
        }
        return _file_meta(file_id, filename=filename, mime=content_type, size_bytes=captured["upload"]["size"])

    async def fake_link(tenant_id, fid, *, entity_kind, entity_id, actor):
        captured["link"] = {
            "tenant_id": tenant_id,
            "file_id": fid,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
        }
        return {"id": str(uuid.uuid4()), "file_id": str(fid),
                "entity_kind": entity_kind, "entity_id": str(entity_id),
                "linked_at": "2026-05-08T01:00:00Z"}

    monkeypatch.setattr(vault_client, "upload", fake_upload)
    monkeypatch.setattr(vault_client, "link", fake_link)

    r = await api_client.post(
        "/api/v1/attachments",
        data={"entity_kind": "bill", "entity_id": str(seed_bill)},
        files={"file": ("receipt.pdf", b"PDF1", "application/pdf")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == str(file_id)
    assert body["filename"] == "receipt.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size"] == 4
    # Vault was called with the right tenant + entity wiring.
    assert captured["upload"]["tenant_id"] == DEFAULT_TENANT_ID
    assert captured["link"]["entity_kind"] == "bill"
    assert captured["link"]["entity_id"] == seed_bill


async def test_upload_unknown_kind_returns_422(
    api_client: AsyncClient,
) -> None:
    r = await api_client.post(
        "/api/v1/attachments",
        data={"entity_kind": "spaceship", "entity_id": str(uuid.uuid4())},
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 422
    assert "spaceship" in r.text


async def test_upload_cross_tenant_entity_returns_403(
    api_client: AsyncClient,
) -> None:
    # A random UUID can't possibly belong to the default tenant.
    bogus = uuid.uuid4()
    r = await api_client.post(
        "/api/v1/attachments",
        data={"entity_kind": "bill", "entity_id": str(bogus)},
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 403


async def test_upload_empty_file_returns_400(
    api_client: AsyncClient,
    seed_bill: uuid.UUID,
) -> None:
    r = await api_client.post(
        "/api/v1/attachments",
        data={"entity_kind": "bill", "entity_id": str(seed_bill)},
        files={"file": ("a.txt", b"", "text/plain")},
    )
    assert r.status_code == 400


async def test_upload_vault_unavailable_returns_502(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    seed_bill: uuid.UUID,
) -> None:
    async def boom(*args, **kwargs):
        raise vault_client.VaultUnavailable("simulated outage")

    monkeypatch.setattr(vault_client, "upload", boom)

    r = await api_client.post(
        "/api/v1/attachments",
        data={"entity_kind": "bill", "entity_id": str(seed_bill)},
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_filters_by_entity(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    seed_bill: uuid.UUID,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list(tenant_id, *, entity_kind=None, entity_id=None,
                        include_archived=False):
        captured["args"] = (tenant_id, entity_kind, entity_id, include_archived)
        return [_file_meta(uuid.uuid4()), _file_meta(uuid.uuid4(), filename="b.pdf")]

    monkeypatch.setattr(vault_client, "list_files", fake_list)

    r = await api_client.get(
        "/api/v1/attachments",
        params={"entity_kind": "bill", "entity_id": str(seed_bill)},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    # Normalised shape — content_type, not mime; size, not size_bytes.
    assert rows[0]["content_type"] == "application/pdf"
    assert rows[0]["size"] == 4
    assert "mime" not in rows[0]
    # Tenant + filters were forwarded.
    assert captured["args"][0] == DEFAULT_TENANT_ID
    assert captured["args"][1] == "bill"
    assert captured["args"][2] == seed_bill


async def test_list_cross_tenant_entity_returns_403(
    api_client: AsyncClient,
) -> None:
    bogus = uuid.uuid4()
    r = await api_client.get(
        "/api/v1/attachments",
        params={"entity_kind": "bill", "entity_id": str(bogus)},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def test_download_streams_bytes(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid.uuid4()

    async def fake_get(tenant_id, fid):
        assert fid == file_id
        return _file_meta(file_id, filename="receipt.pdf", mime="application/pdf")

    async def fake_stream(tenant_id, fid):
        yield b"chunk1", "application/pdf", "receipt.pdf"
        yield b"chunk2", "", ""

    monkeypatch.setattr(vault_client, "get_file", fake_get)
    monkeypatch.setattr(vault_client, "stream_download", fake_stream)

    r = await api_client.get(f"/api/v1/attachments/{file_id}/download")
    assert r.status_code == 200
    assert r.content == b"chunk1chunk2"
    assert r.headers["content-type"].startswith("application/pdf")
    assert "receipt.pdf" in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_returns_204(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid.uuid4()
    seen: dict[str, Any] = {}

    async def fake_delete(tenant_id, fid):
        seen["called"] = (tenant_id, fid)

    monkeypatch.setattr(vault_client, "delete", fake_delete)

    r = await api_client.delete(f"/api/v1/attachments/{file_id}")
    assert r.status_code == 204
    assert seen["called"] == (DEFAULT_TENANT_ID, file_id)


async def test_delete_vault_404_returns_404(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_delete(tenant_id, fid):
        raise vault_client.VaultNotFound("nope")

    monkeypatch.setattr(vault_client, "delete", fake_delete)

    r = await api_client.delete(f"/api/v1/attachments/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Get metadata
# ---------------------------------------------------------------------------


async def test_get_one_returns_normalised_shape(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_id = uuid.uuid4()

    async def fake_get(tenant_id, fid):
        return _file_meta(file_id, filename="x.txt", mime="text/plain")

    monkeypatch.setattr(vault_client, "get_file", fake_get)

    r = await api_client.get(f"/api/v1/attachments/{file_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(file_id)
    assert body["content_type"] == "text/plain"
    assert "mime" not in body
    assert "preview_state" not in body  # dropped intentionally
