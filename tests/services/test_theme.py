"""Tests for ``saebooks.services.theme`` (Wave B / FLAG_THEMES allow-list).

Pure module, no DB — covers the allow-list predicate, the catalogue
payload shape, and the invariant that "default" is always a member (the
tier gate elsewhere in api/v1/users.py relies on that).
"""
from __future__ import annotations

from saebooks.services.theme import (
    ACTIVE_THEMES,
    DEFAULT_THEME_ID,
    THEME_CATALOG,
    is_valid_theme_id,
    theme_catalog_payload,
)


def test_default_theme_is_always_active() -> None:
    assert DEFAULT_THEME_ID in ACTIVE_THEMES


def test_default_theme_id_is_valid() -> None:
    assert is_valid_theme_id(DEFAULT_THEME_ID) is True


def test_none_is_always_valid() -> None:
    """None means "inherit the server-wide theme" — always OK, at every
    tier; the tier gate lives in the caller (api/v1/users.py), not here."""
    assert is_valid_theme_id(None) is True


def test_unknown_theme_id_is_invalid() -> None:
    assert is_valid_theme_id("chartreuse") is False
    assert is_valid_theme_id("") is False


def test_catalog_ids_match_active_themes() -> None:
    assert {t.id for t in THEME_CATALOG} == ACTIVE_THEMES


def test_catalog_entries_are_all_valid() -> None:
    for theme in THEME_CATALOG:
        assert is_valid_theme_id(theme.id) is True


def test_catalog_has_no_duplicate_ids() -> None:
    ids = [t.id for t in THEME_CATALOG]
    assert len(ids) == len(set(ids))


def test_theme_catalog_payload_shape() -> None:
    payload = theme_catalog_payload()
    assert isinstance(payload, list)
    assert len(payload) == len(THEME_CATALOG)
    for entry in payload:
        assert set(entry.keys()) == {"id", "label"}
        assert entry["id"] in ACTIVE_THEMES


def test_theme_catalog_payload_includes_default() -> None:
    payload = theme_catalog_payload()
    ids = {entry["id"] for entry in payload}
    assert DEFAULT_THEME_ID in ids
