"""Unit tests for saebooks.services.integrations.companies_house.enrich.

Pure tests — no HTTP, no DB. Constructs synthetic CH response bodies
and exercises the parse + merge logic.
"""
from __future__ import annotations

import uuid
from typing import Any

from saebooks.models.contact import Contact, ContactType
from saebooks.services.integrations.companies_house.enrich import (
    CompaniesHouseLookup,
    _as_tuple,
    apply_to_contact,
    parse_company_response,
)


def _full_body() -> dict[str, Any]:
    """A realistic CH ``/company/{number}`` response body."""
    return {
        "company_name": "ACME WIDGETS LIMITED",
        "company_number": "12345678",
        "company_status": "active",
        "company_type": "ltd",
        "date_of_creation": "2014-06-06",
        "date_of_cessation": None,
        "jurisdiction": "england-wales",
        "registered_office_address": {
            "address_line_1": "10 Downing Street",
            "address_line_2": "Westminster",
            "locality": "London",
            "region": "Greater London",
            "postal_code": "SW1A 2AA",
            "country": "United Kingdom",
        },
        "sic_codes": ["12345", "67890"],
        "previous_company_names": [
            {
                "name": "OLD ACME LIMITED",
                "ceased_on": "2018-03-15",
                "effective_from": "2014-06-06",
            }
        ],
        "accounts": {
            "next_due": "2026-09-30",
            "last_accounts": {"made_up_to": "2025-03-31", "type": "full"},
        },
    }


def test_parse_full_body_pulls_all_fields() -> None:
    result = parse_company_response(_full_body())
    assert isinstance(result, CompaniesHouseLookup)
    assert result.company_number == "12345678"
    assert result.company_name == "ACME WIDGETS LIMITED"
    assert result.company_status == "active"
    assert result.company_type == "ltd"
    assert result.jurisdiction == "england-wales"
    assert result.date_of_creation == "2014-06-06"
    assert result.date_of_cessation is None
    assert result.address_line1 == "10 Downing Street"
    assert result.address_line2 == "Westminster"
    assert result.address_city == "London"
    assert result.address_state == "Greater London"
    assert result.address_postcode == "SW1A 2AA"
    assert result.address_country == "United Kingdom"
    assert result.sic_codes == ("12345", "67890")
    assert result.previous_names == ("OLD ACME LIMITED",)
    assert result.accounts_next_due == "2026-09-30"
    assert result.accounts_last_made_up_to == "2025-03-31"


def test_parse_empty_body_returns_defaults() -> None:
    result = parse_company_response({})
    assert result.company_number == ""
    assert result.company_name is None
    assert result.sic_codes == ()
    assert result.previous_names == ()


def test_parse_missing_address_and_accounts() -> None:
    body = {
        "company_number": "00000001",
        "company_name": "TINY CO",
    }
    result = parse_company_response(body)
    assert result.address_line1 is None
    assert result.address_city is None
    assert result.accounts_next_due is None
    assert result.accounts_last_made_up_to is None


def test_parse_preserves_raw_for_audit() -> None:
    body = _full_body()
    result = parse_company_response(body)
    assert result.raw == body


def test_as_tuple_handles_none_and_empty() -> None:
    assert _as_tuple(None) == ()
    assert _as_tuple("") == ()
    assert _as_tuple([]) == ()


def test_as_tuple_handles_string_and_list() -> None:
    assert _as_tuple("single") == ("single",)
    assert _as_tuple(["a", "b", ""]) == ("a", "b")
    assert _as_tuple(["  ", "x"]) == ("x",)


def test_as_tuple_pulls_key_from_list_of_dicts() -> None:
    data = [{"name": "Foo"}, {"name": "Bar"}, {"name": ""}, {"other": "skip"}]
    assert _as_tuple(data, key="name") == ("Foo", "Bar")


def test_apply_fills_empty_contact_fields() -> None:
    lookup = parse_company_response(_full_body())
    contact = Contact(
        company_id=uuid.uuid4(),
        name="placeholder",
        contact_type=ContactType.SUPPLIER,
    )
    changed = apply_to_contact(contact, lookup, overwrite=False)
    # name is already "placeholder" (non-empty) — conservative skips
    assert "name" not in changed
    assert contact.address_line1 == "10 Downing Street"
    assert contact.city == "London"
    assert contact.state == "Greater London"
    assert contact.postcode == "SW1A 2AA"
    # address_line1/city/state/postcode all changed
    assert "address_line1" in changed
    assert "city" in changed


def test_apply_overwrite_replaces_populated_values() -> None:
    lookup = parse_company_response(_full_body())
    contact = Contact(
        company_id=uuid.uuid4(),
        name="Old Name Ltd",
        contact_type=ContactType.SUPPLIER,
        city="Sydney",
        state="NSW",
        postcode="2000",
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert "name" in changed
    assert contact.name == "ACME WIDGETS LIMITED"
    assert contact.city == "London"
    assert contact.state == "Greater London"
    assert contact.postcode == "SW1A 2AA"


def test_apply_skips_unchanged_fields() -> None:
    lookup = CompaniesHouseLookup(
        company_number="00000001",
        company_name="Same Co",
        address_city="Same City",
    )
    contact = Contact(
        company_id=uuid.uuid4(),
        name="Same Co",
        contact_type=ContactType.SUPPLIER,
        city="Same City",
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert changed == []


def test_apply_empty_lookup_changes_nothing() -> None:
    lookup = CompaniesHouseLookup(company_number="00000001")
    contact = Contact(
        company_id=uuid.uuid4(),
        name="Original",
        contact_type=ContactType.SUPPLIER,
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert changed == []
    assert contact.name == "Original"


def test_apply_never_persists_ch_specific_fields() -> None:
    """CH-specific fields (company number, SIC, accounts) stay in raw only."""
    lookup = parse_company_response(_full_body())
    contact = Contact(
        company_id=uuid.uuid4(),
        name="",
        contact_type=ContactType.SUPPLIER,
    )
    apply_to_contact(contact, lookup, overwrite=True)
    # Contact model has no company_number/sic_codes columns — merge must
    # not try to setattr those or we'd AttributeError.
    assert not hasattr(contact, "company_number") or getattr(
        contact, "company_number", None
    ) is None
