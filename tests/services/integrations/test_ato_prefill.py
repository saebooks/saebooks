"""Unit tests for saebooks.services.integrations.ato_prefill (stub)."""
from __future__ import annotations

from datetime import date

import pytest

from saebooks.services.integrations.ato_prefill import (
    AtoPrefillError,
    AtoPrefillNotImplementedError,
    BasPrefillResult,
    prefill_bas,
)


async def test_stub_always_raises_not_implemented() -> None:
    with pytest.raises(AtoPrefillNotImplementedError, match="not implemented"):
        await prefill_bas(
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
        )


def test_not_implemented_is_subclass_of_error() -> None:
    assert issubclass(AtoPrefillNotImplementedError, AtoPrefillError)


def test_bas_prefill_result_defaults() -> None:
    result = BasPrefillResult(
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )
    assert result.w1_gross_wages == 0
    assert result.w2_paygw == 0
    assert result.g1_total_sales == 0
    assert result.g11_non_capital_purchases == 0
    assert result.source == "stub"
