"""Unit tests for saebooks.jurisdictions.au.abr.enrich."""
from __future__ import annotations

import uuid

import httpx
import respx

from saebooks.config import Settings
from saebooks.jurisdictions.au.abr.enrich import (
    AbrLookup,
    _format_abn,
    apply_to_contact,
    lookup_abn,
    parse_abr_response,
)
from saebooks.models.contact import Contact, ContactType

ABR_BASE = "https://abr.example/json"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        ABR_API_GUID="test-guid",
        ABR_API_BASE=ABR_BASE,
    )


def _make_contact(**overrides: object) -> Contact:
    """Construct an in-memory Contact — no session, no FK resolution."""
    c = Contact()
    c.id = uuid.uuid4()
    c.company_id = uuid.uuid4()
    c.name = "Temp"
    c.contact_type = ContactType.SUPPLIER
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def test_format_abn_canonical_form() -> None:
    assert _format_abn("51824753556") == "51 824 753 556"


def test_format_abn_returns_input_when_not_11_digits() -> None:
    assert _format_abn("123") == "123"


def test_parse_handles_string_business_name() -> None:
    """ABR sometimes returns a single string instead of a list."""
    result = parse_abr_response(
        {"Abn": "51824753556", "BusinessName": "SAE Engineering"}
    )
    assert result.business_names == ("SAE Engineering",)


def test_parse_handles_list_business_names() -> None:
    result = parse_abr_response(
        {"Abn": "51824753556", "BusinessName": ["A", "B"]}
    )
    assert result.business_names == ("A", "B")


def test_parse_handles_empty_business_name() -> None:
    result = parse_abr_response({"Abn": "51824753556", "BusinessName": []})
    assert result.business_names == ()


def test_parse_marks_gst_registered_when_date_present() -> None:
    result = parse_abr_response(
        {"Abn": "51824753556", "Gst": "2024-02-15"}
    )
    assert result.gst_registered is True
    assert result.gst_from == "2024-02-15"


def test_parse_unregistered_when_gst_empty() -> None:
    result = parse_abr_response({"Abn": "51824753556", "Gst": ""})
    assert result.gst_registered is False


def test_preferred_name_prefers_trading_then_business_then_entity() -> None:
    lookup = AbrLookup(
        abn="x",
        entity_name="Entity",
        business_names=("Biz",),
        trading_names=("Trade",),
    )
    assert lookup.preferred_name == "Trade"

    lookup2 = AbrLookup(abn="x", entity_name="Entity", business_names=("Biz",))
    assert lookup2.preferred_name == "Biz"

    lookup3 = AbrLookup(abn="x", entity_name="Entity")
    assert lookup3.preferred_name == "Entity"


def test_apply_to_contact_only_fills_empty_fields_by_default() -> None:
    contact = _make_contact(name="Existing", state=None, postcode=None)
    lookup = AbrLookup(
        abn="51824753556",
        entity_name="From ABR",
        address_state="QLD",
        address_postcode="4350",
    )
    changed = apply_to_contact(contact, lookup)
    # name already populated -> skipped
    assert "name" not in changed
    assert "state" in changed
    assert "postcode" in changed
    assert "abn" in changed
    assert contact.state == "QLD"
    assert contact.postcode == "4350"
    assert contact.abn == "51 824 753 556"


def test_apply_to_contact_overwrite_replaces_populated_fields() -> None:
    contact = _make_contact(name="Existing", state="NSW")
    lookup = AbrLookup(
        abn="51824753556",
        entity_name="From ABR",
        address_state="QLD",
    )
    changed = apply_to_contact(contact, lookup, overwrite=True)
    assert "name" in changed
    assert "state" in changed
    assert contact.name == "From ABR"
    assert contact.state == "QLD"


def test_apply_to_contact_preserves_canonical_abn_when_unchanged() -> None:
    contact = _make_contact(abn="51 824 753 556")
    lookup = AbrLookup(abn="51824753556")
    changed = apply_to_contact(contact, lookup)
    # already in canonical form -> no change
    assert "abn" not in changed


@respx.mock
async def test_lookup_abn_end_to_end() -> None:
    payload = (
        'callback({'
        '"Abn":"51824753556",'
        '"AbnStatus":"Active",'
        '"EntityName":"Example Pty Ltd",'
        '"BusinessName":["SAE Engineering"],'
        '"AddressState":"QLD",'
        '"AddressPostcode":"4350",'
        '"Gst":"2024-02-15"'
        '})'
    )
    respx.get(f"{ABR_BASE}/AbnDetails.aspx").mock(
        return_value=httpx.Response(200, text=payload)
    )
    result = await lookup_abn("51 824 753 556", settings=_settings())
    assert result.abn == "51824753556"
    assert result.preferred_name == "SAE Engineering"
    assert result.gst_registered is True
    assert result.address_state == "QLD"
