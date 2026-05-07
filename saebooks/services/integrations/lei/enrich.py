"""Parse GLEIF records and merge into Contact rows.

GLEIF's ``/lei-records/{lei}`` returns a JSON:API envelope; after the
client strips it, the shape is roughly::

    {
      "type": "lei-records",
      "id": "529900T8BM49AURSDO55",
      "attributes": {
        "lei": "529900T8BM49AURSDO55",
        "entity": {
          "legalName": {"name": "GlobalBank AG", "language": "en"},
          "legalAddress": {"addressLines": [...], "city": "Frankfurt",
                           "country": "DE", "postalCode": "60327"},
          "headquartersAddress": {...},
          "registeredAs": "HRB 12345",
          "jurisdiction": "DE",
          "category": "GENERAL",
          "legalForm": {"id": "6QQB"},
          "status": "ACTIVE"
        },
        "registration": {
          "status": "ISSUED",
          "initialRegistrationDate": "2014-06-06T19:35:00Z",
          "lastUpdateDate": "2025-04-02T00:00:00Z",
          "nextRenewalDate": "2026-06-05T19:35:00Z",
          "managingLou": "..."
        },
        "bic": ["GBAGDEFFXXX"]
      }
    }

We normalise into :class:`LeiLookup`. Only a subset maps cleanly onto
the existing Contact columns (``name``, ``state``, ``postcode``) — the
rest is displayed in the UI but not persisted. A future migration can
add ``lei``, ``jurisdiction``, ``legal_form``, ``bic`` columns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from saebooks.config import Settings
from saebooks.models.contact import Contact
from saebooks.services.integrations.lei.client import lookup_lei_raw


@dataclass(frozen=True)
class LeiLookup:
    """Normalised GLEIF enrichment payload."""

    lei: str
    legal_name: str | None = None
    jurisdiction: str | None = None
    entity_status: str | None = None  # ACTIVE / INACTIVE / NULL
    registration_status: str | None = None  # ISSUED / LAPSED / etc.
    legal_form: str | None = None
    category: str | None = None
    registered_as: str | None = None
    bic: tuple[str, ...] = ()
    address_city: str | None = None
    address_country: str | None = None
    address_postcode: str | None = None
    address_state: str | None = None
    address_lines: tuple[str, ...] = ()
    initial_registration_date: str | None = None
    last_update_date: str | None = None
    next_renewal_date: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(v) for v in value if str(v).strip())
    return ()


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Chained-dict get — returns ``default`` on any missing key."""
    for key in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
        if obj is None:
            return default
    return obj


def parse_lei_response(data: dict[str, Any]) -> LeiLookup:
    """Parse a GLEIF ``data`` sub-object into :class:`LeiLookup`."""
    attrs = data.get("attributes") or {}
    entity = attrs.get("entity") or {}
    reg = attrs.get("registration") or {}
    address = entity.get("legalAddress") or entity.get("headquartersAddress") or {}

    return LeiLookup(
        lei=str(attrs.get("lei") or data.get("id") or ""),
        legal_name=_get(entity, "legalName", "name"),
        jurisdiction=entity.get("jurisdiction"),
        entity_status=entity.get("status"),
        registration_status=reg.get("status"),
        legal_form=_get(entity, "legalForm", "id"),
        category=entity.get("category"),
        registered_as=entity.get("registeredAs"),
        bic=_as_tuple(attrs.get("bic")),
        address_city=address.get("city"),
        address_country=address.get("country"),
        address_postcode=address.get("postalCode"),
        address_state=address.get("region"),
        address_lines=_as_tuple(address.get("addressLines")),
        initial_registration_date=reg.get("initialRegistrationDate"),
        last_update_date=reg.get("lastUpdateDate"),
        next_renewal_date=reg.get("nextRenewalDate"),
        raw=data,
    )


async def lookup_lei(
    lei: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> LeiLookup:
    """Fetch + parse in one call. Preferred public entry point."""
    raw = await lookup_lei_raw(lei, settings=settings, client=client)
    return parse_lei_response(raw)


def apply_to_contact(
    contact: Contact,
    lookup: LeiLookup,
    *,
    overwrite: bool = False,
) -> list[str]:
    """Merge ``lookup`` into ``contact``. Returns the list of fields changed.

    Conservative by default: only empty fields get filled. Callers that
    want a full refresh pass ``overwrite=True``.

    Only fields the Contact model already has are touched
    (``name``, ``city``, ``state``, ``postcode``, ``address_line1``).
    LEI-specific columns (``lei``, ``jurisdiction``, ``bic``) need a
    separate migration.
    """
    changed: list[str] = []

    def set_field(attr: str, value: str | None) -> None:
        if value is None or value == "":
            return
        current = getattr(contact, attr, None)
        if (overwrite or not current) and current != value:
            setattr(contact, attr, value)
            changed.append(attr)

    set_field("name", lookup.legal_name)
    set_field("city", lookup.address_city)
    set_field("state", lookup.address_state)
    set_field("postcode", lookup.address_postcode)
    if lookup.address_lines:
        set_field("address_line1", lookup.address_lines[0])

    return changed


__all__ = [
    "LeiLookup",
    "apply_to_contact",
    "lookup_lei",
    "parse_lei_response",
]
