"""Intercompany REST router — ``/api/v1/intercompany``.

Covers the freshly-added JSON router that wraps the existing
``services/intercompany.py`` (``post_local_pair`` / ``reverse_local_pair``).
The service + RLS are already covered by ``tests/test_intercompany.py``; this
file pins the HTTP surface:

  * 401 without a bearer token.
  * 201 create posts a reciprocal LOCAL pair (origin=INTERCOMPANY) and returns
    two linked legs.
  * list + get scope to the active company (X-Company-Id) and see the txn from
    either side.
  * reverse flips status to REVERSED; re-reverse -> 409.
  * a missing intercompany edge -> 400 (intercompany_invalid).
  * an unknown id -> 404.

The fixture mirrors ``local_pair_setup`` in ``tests/test_intercompany.py``: two
companies in the DEFAULT tenant with a reciprocal ``IcEdge`` pair and control +
contra accounts, modelling a directors-loan-style LOCAL edge.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

os.environ.setdefault("SAEBOOKS_ENV", "test")

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcLeg,
)
from saebooks.models.journal import EntryStatus, JournalEntry, JournalOrigin

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def ic_setup() -> AsyncIterator[dict[str, Any]]:
    """Two companies in DEFAULT tenant + reciprocal ic_edges + accounts."""
    tag = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        orig = Company(name=f"ICApiOrig-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        cpty = Company(name=f"ICApiCpty-{tag}", base_currency="AUD",
                       tenant_id=_DEFAULT_TENANT)
        session.add_all([orig, cpty])
        await session.flush()

        orig_control = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                               code=f"1-15{tag[:2]}", name="Loan to SAE",
                               account_type=AccountType.ASSET)
        orig_contra = Account(company_id=orig.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        cpty_control = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                               code=f"2-22{tag[:2]}", name="Directors Loan",
                               account_type=AccountType.LIABILITY)
        cpty_contra = Account(company_id=cpty.id, tenant_id=_DEFAULT_TENANT,
                              code=f"1-10{tag[:2]}", name="Bank",
                              account_type=AccountType.ASSET)
        session.add_all([orig_control, orig_contra, cpty_control, cpty_contra])
        await session.flush()

        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=orig.id,
                           partner_company_id=cpty.id,
                           control_account_id=orig_control.id,
                           direction=IcEdgeDirection.ORIGINATOR))
        session.add(IcEdge(tenant_id=_DEFAULT_TENANT, company_id=cpty.id,
                           partner_company_id=orig.id,
                           control_account_id=cpty_control.id,
                           direction=IcEdgeDirection.COUNTERPARTY))
        await session.commit()

        data = {
            "orig_id": orig.id, "cpty_id": cpty.id,
            "orig_control": orig_control.id, "orig_contra": orig_contra.id,
            "cpty_control": cpty_control.id, "cpty_contra": cpty_contra.id,
        }
    yield data

    async with AsyncSessionLocal() as session:
        for cid in (data["orig_id"], data["cpty_id"]):
            await session.execute(text(
                "DELETE FROM ic_legs WHERE company_id = :c"), {"c": cid})
        await session.execute(text(
            "DELETE FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM ic_edges WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM journal_lines WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM journal_entries WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM accounts WHERE company_id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.execute(text(
            "DELETE FROM companies WHERE id IN (:a, :b)"),
            {"a": data["orig_id"], "b": data["cpty_id"]})
        await session.commit()


@pytest_asyncio.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def unauth_client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _create_body(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "originator_company_id": str(d["orig_id"]),
        "counterparty_company_id": str(d["cpty_id"]),
        "amount": "5000.00",
        "entry_date": "2026-06-06",
        "originator_contra_account_id": str(d["orig_contra"]),
        "counterparty_contra_account_id": str(d["cpty_contra"]),
        "description": "Director funds SAE working capital",
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_api_unauth_rejected(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/intercompany")
    assert r.status_code == 401


async def test_api_create_posts_intercompany_origin(
    api_client: AsyncClient, ic_setup: dict[str, Any]
) -> None:
    r = await api_client.post("/api/v1/intercompany", json=_create_body(ic_setup))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "ACTIVE"
    assert len(out["legs"]) == 2
    sides = {leg["side"] for leg in out["legs"]}
    assert sides == {"ORIGINATOR", "COUNTERPARTY"}

    # Prove both legs posted with origin=INTERCOMPANY in the ledger.
    async with AsyncSessionLocal() as session:
        legs = (await session.execute(
            select(IcLeg).where(IcLeg.ic_txn_id == uuid.UUID(out["id"]))
        )).scalars().all()
        assert len(legs) == 2
        for leg in legs:
            je = (await session.execute(
                select(JournalEntry).where(JournalEntry.id == leg.journal_entry_id)
            )).scalar_one()
            assert je.status == EntryStatus.POSTED
            assert je.origin == JournalOrigin.INTERCOMPANY
            assert je.source_type == "ic_txn"
            assert je.source_id == uuid.UUID(out["id"])


async def test_api_list_and_get_from_either_side(
    api_client: AsyncClient, ic_setup: dict[str, Any]
) -> None:
    r = await api_client.post("/api/v1/intercompany", json=_create_body(ic_setup))
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]

    # List as the originator company.
    hdr_orig = {"X-Company-Id": str(ic_setup["orig_id"])}
    r = await api_client.get("/api/v1/intercompany", headers=hdr_orig)
    assert r.status_code == 200, r.text
    assert any(it["id"] == txn_id for it in r.json()["items"])

    # List as the counterparty company — it must see the same txn via its leg.
    hdr_cpty = {"X-Company-Id": str(ic_setup["cpty_id"])}
    r = await api_client.get("/api/v1/intercompany", headers=hdr_cpty)
    assert r.status_code == 200, r.text
    assert any(it["id"] == txn_id for it in r.json()["items"]), (
        "counterparty must see the intercompany txn via its leg"
    )

    # Get the txn with its legs.
    r = await api_client.get(f"/api/v1/intercompany/{txn_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == txn_id
    assert len(r.json()["legs"]) == 2


async def test_api_reverse_then_conflict(
    api_client: AsyncClient, ic_setup: dict[str, Any]
) -> None:
    r = await api_client.post("/api/v1/intercompany", json=_create_body(ic_setup))
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]

    r = await api_client.post(f"/api/v1/intercompany/{txn_id}/reverse", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERSED"

    # Re-reverse -> 409.
    r = await api_client.post(f"/api/v1/intercompany/{txn_id}/reverse", json={})
    assert r.status_code == 409, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "intercompany_not_reversible"


async def test_api_missing_edge_400(
    api_client: AsyncClient, ic_setup: dict[str, Any]
) -> None:
    # Swap originator/counterparty: there is no ORIGINATOR edge on the cpty side,
    # so the service raises IntercompanyError -> 400.
    body = _create_body(ic_setup)
    body["originator_company_id"] = str(ic_setup["cpty_id"])
    body["counterparty_company_id"] = str(ic_setup["orig_id"])
    body["originator_contra_account_id"] = str(ic_setup["cpty_contra"])
    body["counterparty_contra_account_id"] = str(ic_setup["orig_contra"])
    r = await api_client.post("/api/v1/intercompany", json=body)
    assert r.status_code == 400, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "intercompany_invalid"

    # Nothing persisted for either company.
    async with AsyncSessionLocal() as session:
        n_txn = (await session.execute(text(
            "SELECT count(*) FROM ic_txn WHERE company_id IN (:a, :b)"),
            {"a": ic_setup["orig_id"], "b": ic_setup["cpty_id"]})).scalar_one()
    assert n_txn == 0


async def test_api_get_unknown_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/intercompany/{uuid.uuid4()}")
    assert r.status_code == 404, r.text
