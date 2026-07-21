"""Regression: the journal-entry create/update body must accept the header
narration under ``memo`` and ``description`` (not only ``narration``).

A client that POSTed ``{"memo": "..."}`` (the natural field name — the read
shape even calls it ``description``) had the value silently dropped, so the
entry came back with ``description: null``. These backend-agnostic schema tests
pin the input aliasing that fixes the round-trip; the API-level round-trip is
covered in test_journal_entries.py (postgres_only, since JE posting needs the
full ledger stack).
"""
from __future__ import annotations

from datetime import date

import pytest

from saebooks.api.v1.schemas import JournalEntryCreate, JournalEntryUpdate


@pytest.mark.parametrize("field", ["narration", "memo", "description"])
def test_create_accepts_header_narration_aliases(field: str) -> None:
    payload = JournalEntryCreate.model_validate(
        {"entry_date": date(2026, 7, 22), field: "Opening balance setup", "lines": []}
    )
    assert payload.narration == "Opening balance setup"


@pytest.mark.parametrize("field", ["narration", "memo", "description"])
def test_update_accepts_header_narration_aliases(field: str) -> None:
    payload = JournalEntryUpdate.model_validate({field: "Corrected memo"})
    assert payload.narration == "Corrected memo"


def test_create_still_constructs_by_canonical_name() -> None:
    # populate_by_name keeps keyword construction working for internal callers.
    payload = JournalEntryCreate(entry_date=date(2026, 7, 22), narration="x", lines=[])
    assert payload.narration == "x"
