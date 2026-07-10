"""Unit tests for ``saebooks.services.assets_v2_gate.gate_asset_v2_fields``
— the shared FLAG_ASSET_V2 create/update-time check wired into the
fixed-assets create + update routes (Wave A, 2026-07-10).

HTTP-level coverage (through the real routes) lives in
``tests/api/v1/test_fixed_assets.py``; this file exercises the shared
function directly so a future caller (e.g. dispose_partial or the CSV
importer, once either gets a route — see the module docstring) has a
direct example to follow.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from saebooks.db import AsyncSessionLocal
from saebooks.services.assets_v2_gate import gate_asset_v2_fields

pytestmark = pytest.mark.postgres_only


def _fake_request() -> SimpleNamespace:
    """Minimal stand-in for ``fastapi.Request`` — see test_fx_gate.py
    for why this is a faithful substitute for
    ``_effective_edition_for_request``'s purposes."""
    return SimpleNamespace(state=SimpleNamespace(user=None))


async def test_linear_model_no_tax_split_never_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        await gate_asset_v2_fields(
            session,
            _fake_request(),
            depreciation_model_id="asset_5_year_linear",
            tax_model_id=None,
        )  # must not raise


async def test_dv_book_model_gated_at_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await gate_asset_v2_fields(
                session,
                _fake_request(),
                depreciation_model_id="asset_dv_30",
                tax_model_id=None,
            )
    assert exc_info.value.status_code == 404


async def test_dv_book_model_ungated_at_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "offline")
    async with AsyncSessionLocal() as session:
        await gate_asset_v2_fields(
            session,
            _fake_request(),
            depreciation_model_id="asset_dv_30",
            tax_model_id=None,
        )  # must not raise


async def test_tax_split_gated_even_with_linear_tax_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting tax_model_id at all is v2, even when it points at a
    linear model — the split itself is the paid feature."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        with pytest.raises(HTTPException) as exc_info:
            await gate_asset_v2_fields(
                session,
                _fake_request(),
                depreciation_model_id="asset_10_year_linear",
                tax_model_id="asset_5_year_linear",
            )
    assert exc_info.value.status_code == 404


async def test_neither_field_present_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH payloads that don't touch either field (both None,
    e.g. a cosmetic-only update) never gate, at any tier."""
    from saebooks.config import settings as _s

    monkeypatch.setattr(_s, "edition", "community")
    async with AsyncSessionLocal() as session:
        await gate_asset_v2_fields(
            session,
            _fake_request(),
            depreciation_model_id=None,
            tax_model_id=None,
        )  # must not raise
