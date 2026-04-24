"""Tier-5 report tests — /api/v1/reports/pl_by_segment (cycle 27).

project_id IS present on JournalLine → happy path implemented.

2 tests:
* test_pl_by_segment_unsupported_type_returns_501
* test_pl_by_segment_project_groups_by_project_id
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
async def gl_accounts() -> dict[str, str]:
    """Return one account ID per relevant AccountType for building JE payloads."""
    async with AsyncSessionLocal() as session:
        result: dict[str, str] = {}
        for at in (
            AccountType.INCOME,
            AccountType.EXPENSE,
            AccountType.ASSET,
        ):
            row = (
                await session.execute(
                    select(Account).where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                    ).limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_project(client: AsyncClient, name: str = "Test Segment Project") -> str:
    """Create a project via the API and return its id."""
    r = await client.post(
        "/api/v1/projects",
        json={
            "code": f"SEG-{uuid.uuid4().hex[:6].upper()}",
            "name": name,
            "status": "ACTIVE",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_and_post_je(
    client: AsyncClient,
    entry_date: str,
    lines: list[dict],
) -> dict:
    """Create a DRAFT JE then PATCH to POSTED. Return posted body."""
    r = await client.post(
        "/api/v1/journal_entries",
        json={
            "entry_date": entry_date,
            "narration": "P&L segment test entry",
            "lines": lines,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    je_id = body["id"]
    version = body["version"]

    r2 = await client.patch(
        f"/api/v1/journal_entries/{je_id}",
        json={"status": "POSTED"},
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_pl_by_segment_unsupported_type_returns_501(
    api_client: AsyncClient,
) -> None:
    """Requesting segment_type other than 'project' returns HTTP 501."""
    r = await api_client.get(
        "/api/v1/reports/pl_by_segment",
        params={
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "segment_type": "department",
        },
    )
    assert r.status_code == 501, r.text


async def test_pl_by_segment_project_groups_by_project_id(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
) -> None:
    """POSTED JEs with project_id tag appear grouped by project in the report."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Create a project to tag lines with
    project_id = await _create_project(api_client, "Segment Alpha")

    # Post a JE where the income line is tagged with the project
    await _create_and_post_je(
        api_client,
        "2028-03-20",
        lines=[
            {"account_id": asset_id, "debit": "6000.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "6000.00",
                "project_id": project_id,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/pl_by_segment",
        params={
            "from_date": "2028-01-01",
            "to_date": "2028-12-31",
            "segment_type": "project",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["segment_type"] == "project"
    assert isinstance(body["segments"], list)
    assert len(body["segments"]) > 0, "Expected at least one segment in report"

    # Find the segment matching our project
    project_segments = [
        s for s in body["segments"] if s["segment_id"] == project_id
    ]
    assert project_segments, (
        f"Project {project_id} not found in segments: "
        f"{[s['segment_id'] for s in body['segments']]}"
    )

    seg = project_segments[0]
    # Each segment has sections with account lines
    assert "sections" in seg
    assert "net_profit" in seg

    # The income section should contain our tagged account
    income_sections = [
        s for s in seg["sections"] if s["account_type"] == "INCOME"
    ]
    assert income_sections, "No INCOME section in segment"
    income_lines = income_sections[0]["lines"]
    matching = [l for l in income_lines if l["account_id"] == income_id]
    assert matching, "Tagged INCOME account not found in project segment"
    assert matching[0]["amount"] >= 6000.0
