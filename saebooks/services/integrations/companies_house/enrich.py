"""Parse Companies House records and merge into Contact rows.

The CH ``/company/{number}`` response is a flat JSON object (unlike
GLEIF's JSON:API envelope). A sample shape::

    {
      "company_name": "ACME WIDGETS LIMITED",
      "company_number": "12345678",
      "company_status": "active",
      "company_type": "ltd",
      "date_of_creation": "2014-06-06",
      "date_of_cessation": null,
      "jurisdiction": "england-wales",
      "registered_office_address": {
        "address_line_1": "10 Downing Street",
        "address_line_2": "Westminster",
        "locality": "London",
        "region": "Greater London",
        "postal_code": "SW1A 2AA",
        "country": "United Kingdom"
      },
      "sic_codes": ["12345", "67890"],
      "previous_company_names": [{"name": "OLD ACME LIMITED",
                                  "ceased_on": "2018-03-15",
                                  "effective_from": "2014-06-06"}],
      "accounts": {
        "next_due": "2026-09-30",
        "last_accounts": {"made_up_to": "2025-03-31", "type": "full"}
      }
    }

We normalise into :class:`CompaniesHouseLookup`. Only fields that map
cleanly onto the existing Contact columns (``name``, ``address_line1``,
``city``, ``state``, ``postcode``) are persisted on merge. The rest
(company number, status, SIC codes, accounts due dates) is shown in
the preview card and kept in ``raw`` for audit but not persisted —
adding dedicated columns is a future additive migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from saebooks.config import Settings
from saebooks.models.contact import Contact
from saebooks.services.integrations.companies_house.client import (
    lookup_company_raw,
)


@dataclass(frozen=True)
class CompaniesHouseLookup:
    """Normalised Companies House enrichment payload."""

    company_number: str
    company_name: str | None = None
    company_status: str | None = None  # active / dissolved / liquidation / etc.
    company_type: str | None = None  # ltd / plc / llp / etc.
    jurisdiction: str | None = None  # england-wales / scotland / northern-ireland
    date_of_creation: str | None = None
    date_of_cessation: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_postcode: str | None = None
    address_country: str | None = None
    sic_codes: tuple[str, ...] = ()
    previous_names: tuple[str, ...] = ()
    accounts_next_due: str | None = None
    accounts_last_made_up_to: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _as_tuple(value: Any, *, key: str | None = None) -> tuple[str, ...]:
    """Coerce ``value`` into a tuple of non-empty strings.

    Handles: ``None`` → ``()``, plain ``str`` → single-element tuple,
    ``list[str]`` → filtered tuple, ``list[dict]`` with ``key`` → tuple
    of the referenced sub-field.
    """
    if not value:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if key and isinstance(item, dict):
                v = item.get(key)
                if v and str(v).strip():
                    out.append(str(v))
            elif not key:
                s = str(item)
                if s.strip():
                    out.append(s)
        return tuple(out)
    return ()


def parse_company_response(data: dict[str, Any]) -> CompaniesHouseLookup:
    """Parse a Companies House ``/company/{number}`` body into a lookup."""
    address = data.get("registered_office_address") or {}
    accounts = data.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}

    return CompaniesHouseLookup(
        company_number=str(data.get("company_number") or ""),
        company_name=data.get("company_name"),
        company_status=data.get("company_status"),
        company_type=data.get("company_type"),
        jurisdiction=data.get("jurisdiction"),
        date_of_creation=data.get("date_of_creation"),
        date_of_cessation=data.get("date_of_cessation"),
        address_line1=address.get("address_line_1"),
        address_line2=address.get("address_line_2"),
        address_city=address.get("locality"),
        address_state=address.get("region"),
        address_postcode=address.get("postal_code"),
        address_country=address.get("country"),
        sic_codes=_as_tuple(data.get("sic_codes")),
        previous_names=_as_tuple(
            data.get("previous_company_names"), key="name"
        ),
        accounts_next_due=accounts.get("next_due"),
        accounts_last_made_up_to=last_accounts.get("made_up_to"),
        raw=data,
    )


async def lookup_company(
    number: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> CompaniesHouseLookup:
    """Fetch + parse in one call. Preferred public entry point."""
    raw = await lookup_company_raw(number, settings=settings, client=client)
    return parse_company_response(raw)


def apply_to_contact(
    contact: Contact,
    lookup: CompaniesHouseLookup,
    *,
    overwrite: bool = False,
) -> list[str]:
    """Merge ``lookup`` into ``contact``. Returns the fields actually changed.

    Conservative by default: only empty Contact fields get filled. Pass
    ``overwrite=True`` to replace populated values.

    Only fields present on the Contact model today are touched
    (``name``, ``address_line1``, ``city``, ``state``, ``postcode``).
    CH-specific columns (company number, status, SIC codes) need a
    separate additive migration to persist.
    """
    changed: list[str] = []

    def set_field(attr: str, value: str | None) -> None:
        if value is None or value == "":
            return
        current = getattr(contact, attr, None)
        if (overwrite or not current) and current != value:
            setattr(contact, attr, value)
            changed.append(attr)

    set_field("name", lookup.company_name)
    set_field("address_line1", lookup.address_line1)
    set_field("city", lookup.address_city)
    set_field("state", lookup.address_state)
    set_field("postcode", lookup.address_postcode)

    return changed


__all__ = [
    "CompaniesHouseLookup",
    "apply_to_contact",
    "lookup_company",
    "parse_company_response",
]
