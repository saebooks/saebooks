"""Tests for the CoA template registry / dispatcher."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.services.templates import (
    UnknownTemplate,
    apply_template,
    known_jurisdictions,
    known_templates,
)


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        return co.id


def test_registry_lists_all_five_jurisdictions() -> None:
    keys = known_templates()
    assert "au/default" in keys
    assert "nz/default" in keys
    assert "uk/default" in keys
    assert "ee/default" in keys
    assert "xx/default" in keys


def test_known_jurisdictions_excludes_unbuilt_stubs() -> None:
    """known_jurisdictions() is CompanyCreate's validation source of truth
    — a jurisdiction is creatable iff it has an IMPLEMENTED default chart
    template. AU/EE/XX are built; NZ/UK have only stub templates and
    LT/LV none at all (their tax engines are live on this head, but a
    company with no chart of accounts is not a working state), so none of
    NZ/UK/LT/LV may be offered as a creatable jurisdiction."""
    known = known_jurisdictions()
    assert "AU" in known
    assert "EE" in known
    assert "XX" in known
    assert "NZ" not in known
    assert "UK" not in known
    assert "LT" not in known
    assert "LV" not in known


async def test_apply_nz_default_raises_not_implemented() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotImplementedError, match="M1"):
            await apply_template(session, company_id, "nz/default")


async def test_apply_uk_default_raises_not_implemented() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotImplementedError, match="M2"):
            await apply_template(session, company_id, "uk/default")


async def _create_ee_company(session, name: str) -> uuid.UUID:
    from saebooks.services.companies import create_company

    company = await create_company(
        session,
        name=name,
        jurisdiction="EE",
        coa_template_key="ee/default",
    )
    return company.id


async def _delete_company(name: str) -> None:
    from sqlalchemy import delete

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Company).where(Company.name == name))
        await session.commit()


async def test_apply_ee_default_creates_accounts_matching_reference_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account codes created == the template's own code set (count +
    spot codes incl. control accounts) — no hardcoded magic count."""
    from saebooks.config import settings as app_settings
    from saebooks.jurisdictions.ee.chart import known_chart_row_codes
    from saebooks.models.account import Account, AccountType

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    expected_codes = await known_chart_row_codes()
    assert expected_codes, "reference/fallback template must not be empty"

    # Guard against chart.py's embedded fallback silently drifting
    # from the yaml it's a lock-step copy of (REFERENCE_DATABASE_URL is
    # unset in this harness, so the fallback — not the reference DB —
    # is what the applier actually reads below; without this the
    # account-set assertion would only prove the applier matches
    # itself, not the reference template).
    import yaml

    yaml_path = (
        Path(__file__).resolve().parents[2]
        / "saebooks"
        / "seeds"
        / "jurisdictions"
        / "EE"
        / "chart_template.yaml"
    )
    with yaml_path.open() as f:
        yaml_codes = {row["account_code"] for row in yaml.safe_load(f)["rows"]}
    assert expected_codes == yaml_codes, (
        "jurisdictions/ee/chart.py _EMBEDDED_FALLBACK has drifted from "
        "seeds/jurisdictions/EE/chart_template.yaml"
    )

    name = f"EE Chart Test {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            company_id = await _create_ee_company(session, name)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Account).where(Account.company_id == company_id)
            )
            accounts = {a.code: a for a in result.scalars().all()}

        assert set(accounts) == expected_codes

        ar = accounts["1200"]
        assert ar.account_type == AccountType.ASSET
        assert "Accounts Receivable" in ar.name

        ap = accounts["2100"]
        assert ap.account_type == AccountType.LIABILITY
        assert "Accounts Payable" in ap.name
    finally:
        await _delete_company(name)


async def test_apply_ee_default_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-applying the EE template to the same company inserts no dupes."""
    from saebooks.config import settings as app_settings
    from saebooks.models.account import Account
    from saebooks.services.templates import apply_template

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"EE Chart Idempotent {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            company_id = await _create_ee_company(session, name)

        async def _count() -> int:
            async with AsyncSessionLocal() as session:
                return (
                    await session.execute(
                        select(func.count()).select_from(Account).where(
                            Account.company_id == company_id
                        )
                    )
                ).scalar_one()

        count_before = await _count()

        async with AsyncSessionLocal() as session:
            await apply_template(session, company_id, "ee/default")

        assert await _count() == count_before
    finally:
        await _delete_company(name)


async def test_apply_ee_default_sets_control_account_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"EE Chart Control Accts {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            company_id = await _create_ee_company(session, name)

        async with AsyncSessionLocal() as session:
            company = await session.get(Company, company_id)
            assert company.ar_control_account_code == "1200"
            assert company.ap_control_account_code == "2100"
    finally:
        await _delete_company(name)


async def test_apply_au_default_untouched_by_ee_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AU company creation still does NOT auto-apply any template (the
    create_company wiring dispatches for any coa_template_key != 'au/default'
    — the legacy AU seed/CLI path is unaffected)."""
    from saebooks.config import settings as app_settings
    from saebooks.models.account import Account
    from saebooks.services.companies import create_company

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"AU Untouched Test {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            # Explicit AU (the core create_company default is now the
            # neutral "XX"/"xx/default"; this test is about the AU carve-out).
            company = await create_company(
                session, name=name, jurisdiction="AU", coa_template_key="au/default"
            )
            assert company.coa_template_key == "au/default"
            count = (
                await session.execute(
                    select(func.count()).select_from(Account).where(
                        Account.company_id == company.id
                    )
                )
            ).scalar_one()
            assert count == 0
    finally:
        await _delete_company(name)


async def test_create_company_nz_template_raises_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_company() dispatches via the generic registry, not a
    hardcoded "ee/default" string — an unimplemented-but-registered key
    now fails loudly instead of being silently persisted with no chart
    of accounts (critic round 1, finding 1). Calls the service directly
    (not the API) since it "trusts its caller" per its own docstring —
    the API-layer guard is a separate fix (finding 3)."""
    from saebooks.config import settings as app_settings
    from saebooks.services.companies import create_company

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"NZ Stub Test {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(NotImplementedError, match="M1"):
                await create_company(
                    session,
                    name=name,
                    jurisdiction="NZ",
                    coa_template_key="nz/default",
                )
        # Critic round 2, finding 2/4: the failed template application must
        # not leave an orphaned Company row committed.
        async with AsyncSessionLocal() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(Company).where(Company.name == name)
                )
            ).scalar_one()
            assert count == 0
    finally:
        await _delete_company(name)


async def test_create_company_garbage_template_raises_unknown_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd/garbage coa_template_key fails loudly (UnknownTemplate)
    instead of being silently persisted verbatim (critic round 1,
    finding 1)."""
    from saebooks.config import settings as app_settings
    from saebooks.services.companies import create_company

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"Garbage Template Test {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(UnknownTemplate):
                await create_company(
                    session,
                    name=name,
                    coa_template_key="ee/defualt",
                )
        # Critic round 2, finding 2/4: no orphaned, chart-less company row
        # left behind by the failed template application.
        async with AsyncSessionLocal() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(Company).where(Company.name == name)
                )
            ).scalar_one()
            assert count == 0
    finally:
        await _delete_company(name)


async def test_apply_xx_default_creates_company_with_zero_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critic round 2, finding 1: the neutral sentinel "XX" is advertised
    by known_jurisdictions() as a working jurisdiction, so company
    creation against it (jurisdiction="XX", coa_template_key="xx/default")
    must actually succeed instead of dead-ending in UnknownTemplate."""
    from saebooks.config import settings as app_settings
    from saebooks.models.account import Account
    from saebooks.services.companies import create_company

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    name = f"XX Neutral Test {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as session:
            company = await create_company(
                session,
                name=name,
                jurisdiction="XX",
                coa_template_key="xx/default",
            )
            assert company.jurisdiction == "XX"
            count = (
                await session.execute(
                    select(func.count()).select_from(Account).where(
                        Account.company_id == company.id
                    )
                )
            ).scalar_one()
            assert count == 0
    finally:
        await _delete_company(name)


async def test_apply_unknown_template_raises_unknown() -> None:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(UnknownTemplate):
            await apply_template(session, company_id, "moon/default")


async def test_seed_company_has_au_default_template_key() -> None:
    """0103 backfill should leave the seed company at au/default."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        assert co.coa_template_key == "au/default"
