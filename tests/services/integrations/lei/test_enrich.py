"""Unit tests for saebooks.services.integrations.lei.enrich.

Pure tests — no HTTP, no DB. Constructs synthetic GLEIF envelopes and
validates the parse + merge logic.
"""
from __future__ import annotations

from typing import Any

from saebooks.models.contact import Contact, ContactType
from saebooks.services.integrations.lei.enrich import (
    LeiLookup,
    apply_to_contact,
    parse_lei_response,
)


def _full_envelope() -> dict[str, Any]:
    """A realistic GLEIF ``data`` envelope."""
    return {
        "type": "lei-records",
        "id": "529900T8BM49AURSDO55",
        "attributes": {
            "lei": "529900T8BM49AURSDO55",
            "entity": {
                "legalName": {"name": "GlobalBank AG", "language": "en"},
                "legalAddress": {
                    "addressLines": ["Hauptstrasse 1"],
                    "city": "Frankfurt",
                    "country": "DE",
                    "postalCode": "60327",
                    "region": "DE-HE",
                },
                "headquartersAddress": {
                    "addressLines": ["Other line"],
                    "city": "Berlin",
                    "country": "DE",
                },
                "jurisdiction": "DE",
                "category": "GENERAL",
                "legalForm": {"id": "6QQB"},
                "registeredAs": "HRB 12345",
                "status": "ACTIVE",
            },
            "registration": {
                "status": "ISSUED",
                "initialRegistrationDate": "2014-06-06T19:35:00Z",
                "lastUpdateDate": "2025-04-02T00:00:00Z",
                "nextRenewalDate": "2026-06-05T19:35:00Z",
            },
            "bic": ["GBAGDEFFXXX"],
        },
    }


def test_parse_full_envelope_pulls_all_fields() -> None:
    result = parse_lei_response(_full_envelope())
    assert isinstance(result, LeiLookup)
    assert result.lei == "529900T8BM49AURSDO55"
    assert result.legal_name == "GlobalBank AG"
    assert result.jurisdiction == "DE"
    assert result.entity_status == "ACTIVE"
    assert result.registration_status == "ISSUED"
    assert result.legal_form == "6QQB"
    assert result.category == "GENERAL"
    assert result.registered_as == "HRB 12345"
    assert result.bic == ("GBAGDEFFXXX",)
    assert result.address_city == "Frankfurt"
    assert result.address_country == "DE"
    assert result.address_postcode == "60327"
    assert result.address_state == "DE-HE"
    assert result.address_lines == ("Hauptstrasse 1",)
    assert result.initial_registration_date.startswith("2014-06-06")
    assert result.next_renewal_date.startswith("2026-06-05")


def test_parse_falls_back_to_headquarters_when_legal_address_absent() -> None:
    env = _full_envelope()
    del env["attributes"]["entity"]["legalAddress"]
    result = parse_lei_response(env)
    assert result.address_city == "Berlin"
    assert result.address_lines == ("Other line",)


def test_parse_empty_envelope_returns_defaults() -> None:
    result = parse_lei_response({"attributes": {}})
    assert result.lei == ""  # no id, no attributes.lei — empty
    assert result.legal_name is None
    assert result.bic == ()
    assert result.address_lines == ()


def test_parse_lei_uses_id_when_attributes_lei_missing() -> None:
    env = {"id": "529900T8BM49AURSDO55", "attributes": {}}
    result = parse_lei_response(env)
    assert result.lei == "529900T8BM49AURSDO55"


def test_apply_fills_empty_contact_fields() -> None:
    lookup = parse_lei_response(_full_envelope())
    contact = Contact(
        company_id=_uuid(),
        name="placeholder",
        contact_type=ContactType.SUPPLIER,
    )
    changed = apply_to_contact(contact, lookup, overwrite=False)
    # name is not empty ("placeholder") so conservative merge skips it
    assert "name" not in changed
    assert contact.city == "Frankfurt"
    assert contact.state == "DE-HE"
    assert contact.postcode == "60327"
    assert contact.address_line1 == "Hauptstrasse 1"


def test_apply_overwrite_replaces_populated_values() -> None:
    lookup = parse_lei_response(_full_envelope())
    contact = Contact(
        company_id=_uuid(),
        name="Placeholder Pty Ltd",
        contact_type=ContactType.SUPPLIER,
        city="Sydney",
        state="NSW",
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert "name" in changed
    assert contact.name == "GlobalBank AG"
    assert contact.city == "Frankfurt"
    assert contact.state == "DE-HE"


def test_apply_skips_unchanged_fields() -> None:
    lookup = LeiLookup(lei="X", legal_name="Same", address_city="Same City")
    contact = Contact(
        company_id=_uuid(),
        name="Same",
        contact_type=ContactType.SUPPLIER,
        city="Same City",
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert changed == []


def test_apply_empty_lookup_changes_nothing() -> None:
    lookup = LeiLookup(lei="X")
    contact = Contact(
        company_id=_uuid(),
        name="Original",
        contact_type=ContactType.SUPPLIER,
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert changed == []
    assert contact.name == "Original"


def _uuid() -> Any:
    import uuid

    return uuid.uuid4()
