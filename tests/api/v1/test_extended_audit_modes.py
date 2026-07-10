"""Contract tests for extended_audit_modes (Wave C, FLAG_EXTENDED_AUDIT_MODES).

Covers the LIVE ``PATCH /api/v1/journal_entries/{id}`` path (NOT the
dead ``services.journal.update_draft`` — see that module's docstring
for why testing only the dead path would pin nothing real):

* immutable (default, every edition): editing a POSTED entry is
  blocked, original data unchanged.
* open (Offline+): editing a POSTED entry succeeds and writes an
  audit_snapshots before/after row.
* hybrid (Offline+): editable before period-close, blocked after —
  reuses the F-04 period-lock override path (admin + reason).
* Fail-safe: a company with a non-immutable ``audit_mode`` stored but
  running at an edition below Offline still behaves immutable — the
  tier gate is re-derived at edit time, not trusted from the column.
* ``company.audit_mode`` write gate on ``PATCH /api/v1/companies/{id}``
  — 404 below Offline, 200 at Offline+.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.audit_snapshot import AuditSnapshot
from saebooks.models.company import Company
from saebooks.services import journal as journal_svc

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_company(audit_mode: str) -> dict[str, str]:
    """Create an isolated company + two accounts under the default
    tenant, with ``audit_mode`` set directly (bypassing the API gate —
    this is test setup, not a claim the value was legitimately earned).
    """
    cid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=cid,
                tenant_id=_DEFAULT_TENANT_ID,
                name=f"AuditMode {audit_mode} {cid.hex[:6]}",
                audit_mode=audit_mode,
            )
        )
        await session.flush()
        asset = Account(
            company_id=cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"1-{cid.hex[:4]}", name="Asset", account_type=AccountType.ASSET,
            is_header=False,
        )
        expense = Account(
            company_id=cid, tenant_id=_DEFAULT_TENANT_ID,
            code=f"6-{cid.hex[:4]}", name="Expense", account_type=AccountType.EXPENSE,
            is_header=False,
        )
        session.add_all([asset, expense])
        await session.flush()
        await session.refresh(asset)
        await session.refresh(expense)
        await session.commit()
        return {
            "company_id": str(cid),
            "asset_id": str(asset.id),
            "expense_id": str(expense.id),
        }


async def _client_for(company_id: str) -> AsyncClient:
    token = current_token()
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": company_id,
        },
    )


async def _post_and_post_entry(
    client: AsyncClient, ids: dict[str, str], *, entry_date: str = "2026-04-01"
) -> tuple[str, int]:
    """Create + post a balanced JE, return (entry_id, version-after-post)."""
    payload = {
        "entry_date": entry_date,
        "narration": "Original narration",
        "lines": [
            {"account_id": ids["asset_id"], "debit": "100.00", "credit": "0.00"},
            {"account_id": ids["expense_id"], "debit": "0.00", "credit": "100.00"},
        ],
    }
    rc = await client.post("/api/v1/journal_entries", json=payload)
    assert rc.status_code == 201, rc.text
    entry_id = rc.json()["id"]
    version = rc.json()["version"]
    rp = await client.post(
        f"/api/v1/journal_entries/{entry_id}/post",
        headers={"If-Match": str(version)},
    )
    assert rp.status_code == 200, rp.text
    return entry_id, rp.json()["version"]


# --------------------------------------------------------------------------- #
# Immutable (default / Community) — blocks editing a POSTED entry.            #
# --------------------------------------------------------------------------- #


async def test_immutable_blocks_posted_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")

    ids = await _make_company("immutable")
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(client, ids)

        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"narration": "Attempted edit"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 422, r.text
        assert "immutable" in r.text.lower()

        rg = await client.get(f"/api/v1/journal_entries/{entry_id}")
        assert rg.json()["description"] == "Original narration"


async def test_immutable_blocks_status_flip_via_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATCH that tries to flip status is refused regardless of value
    (Wave C fix — status transitions must go through /post or /reverse)."""
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")

    ids = await _make_company("immutable")
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(client, ids)

        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"status": "DRAFT"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# Open (Offline+) — editable, every edit logged.                              #
# --------------------------------------------------------------------------- #


async def test_open_allows_posted_edit_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "offline")

    ids = await _make_company("open")
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(client, ids)

        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"narration": "Edited under open mode"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["description"] == "Edited under open mode"

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(AuditSnapshot).where(
                    AuditSnapshot.table_name == "journal_entries",
                    AuditSnapshot.row_id == entry_id,
                    AuditSnapshot.action == "update",
                )
            )
        ).scalars().all()
        assert rows, "open-mode edit must write an audit_snapshots before/after row"
        assert rows[-1].before_data.get("description") == "Original narration"


async def test_open_mode_entry_date_change_is_422_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: migration 0161's je_engine_guard trigger
    unconditionally refuses to change entry_date/ref on a POSTED entry
    at the DB layer, for every role, regardless of audit mode. Without
    an application-level pre-check this would surface as an uncaught
    IntegrityError -> 500 the first time open/hybrid mode let a caller
    reach the mutation. Narration-only edits (tested above) don't
    exercise this path — this test specifically changes entry_date."""
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "offline")

    ids = await _make_company("open")
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(client, ids)

        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"entry_date": "2026-04-15"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 422, (
            f"expected a clean 422 (DB-layer identity-field guard), got "
            f"{r.status_code}: {r.text}"
        )
        assert "entry_date" in r.text.lower()

        # Same-value entry_date (no-op) must still succeed — only an
        # actual change is refused.
        rg = await client.get(f"/api/v1/journal_entries/{entry_id}")
        current_date = rg.json()["entry_date"]
        r2 = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"entry_date": current_date, "narration": "still editable"},
            headers={"If-Match": str(version)},
        )
        assert r2.status_code == 200, r2.text


# --------------------------------------------------------------------------- #
# Hybrid (Offline+) — editable pre-close, blocked post-close.                 #
# --------------------------------------------------------------------------- #


async def test_hybrid_allows_edit_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "offline")

    ids = await _make_company("hybrid")
    async with await _client_for(ids["company_id"]) as client:
        # Entry dated after any lock (none set) — hybrid mode with no
        # period lock at all behaves like open for this entry's date.
        entry_id, version = await _post_and_post_entry(
            client, ids, entry_date="2026-05-15"
        )
        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"narration": "Edited pre-close"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 200, r.text


async def test_hybrid_blocks_edit_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "offline")

    ids = await _make_company("hybrid")
    company_id = uuid.UUID(ids["company_id"])
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(
            client, ids, entry_date="2026-03-15"
        )

        async with AsyncSessionLocal() as session:
            await journal_svc.lock_period(
                session, company_id, date(2026, 3, 31), locked_by="test-wave-c"
            )

        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"narration": "Attempted post-close edit"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 422, r.text
        assert "lock" in r.text.lower() or "period" in r.text.lower()

        # F-04 override path: admin + real reason still gets through.
        r2 = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={
                "narration": "Admin override edit",
                "override_reason": "Correcting a coding error found in year-end review",
            },
            headers={"If-Match": str(version)},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["description"] == "Admin override edit"


# --------------------------------------------------------------------------- #
# Fail-safe: stored non-immutable value ignored when not entitled.            #
# --------------------------------------------------------------------------- #


async def test_non_immutable_stored_value_ignored_below_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A company whose audit_mode column already says "open" (e.g. stale
    data from before this wave, or a downgraded licence) must NOT get
    open-mode behaviour once the caller's edition drops below Offline —
    effective_audit_mode() re-derives entitlement, it doesn't trust the
    stored column value.
    """
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")

    ids = await _make_company("open")  # stored value says open...
    async with await _client_for(ids["company_id"]) as client:
        entry_id, version = await _post_and_post_entry(client, ids)
        # ...but edition is community (< Offline) => still immutable.
        r = await client.patch(
            f"/api/v1/journal_entries/{entry_id}",
            json={"narration": "Should be blocked"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 422, r.text
        assert "immutable" in r.text.lower()


# --------------------------------------------------------------------------- #
# company.audit_mode write gate — PATCH /api/v1/companies/{id}.               #
# --------------------------------------------------------------------------- #


async def test_writing_open_audit_mode_404_below_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")

    ids = await _make_company("immutable")
    async with await _client_for(ids["company_id"]) as client:
        rg = await client.get(f"/api/v1/companies/{ids['company_id']}")
        version = rg.json()["version"]
        r = await client.patch(
            f"/api/v1/companies/{ids['company_id']}",
            json={"audit_mode": "open"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 404, r.text


async def test_writing_open_audit_mode_200_at_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "offline")

    ids = await _make_company("immutable")
    async with await _client_for(ids["company_id"]) as client:
        rg = await client.get(f"/api/v1/companies/{ids['company_id']}")
        version = rg.json()["version"]
        r = await client.patch(
            f"/api/v1/companies/{ids['company_id']}",
            json={"audit_mode": "open"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["audit_mode"] == "open"


async def test_writing_immutable_audit_mode_never_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting audit_mode back to immutable (the free default) must
    never 404, even at Community — only non-immutable values cross the
    tier boundary."""
    from saebooks.config import settings as module_settings
    monkeypatch.setattr(module_settings, "edition", "community")

    ids = await _make_company("immutable")
    async with await _client_for(ids["company_id"]) as client:
        rg = await client.get(f"/api/v1/companies/{ids['company_id']}")
        version = rg.json()["version"]
        r = await client.patch(
            f"/api/v1/companies/{ids['company_id']}",
            json={"audit_mode": "immutable"},
            headers={"If-Match": str(version)},
        )
        assert r.status_code == 200, r.text
