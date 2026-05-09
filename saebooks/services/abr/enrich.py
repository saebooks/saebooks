"""Parse ABR responses and apply them to Contact records.

The raw ABR JSON shape (circa 2026) looks roughly like::

    {
      "Abn": "87744586592",
      "AbnStatus": "Active",
      "AbnStatusEffectiveFrom": "2024-02-15",
      "Acn": "683275756",
      "AddressDate": "2024-02-15",
      "AddressPostcode": "4350",
      "AddressState": "QLD",
      "BusinessName": [],
      "EntityName": "Sauer Pty Ltd ATF Saueesti Trust",
      "EntityTypeCode": "DIT",
      "EntityTypeName": "Discretionary Investment Trust",
      "Gst": "2024-02-15",
      "Message": ""
    }

We normalise that into :class:`AbrLookup`, a stable surface that won't
shift if ABR reshuffles their field names. The UI binds to the
dataclass; only this module cares about the raw shape.

The write-back into :class:`Contact` is conservative: we only overwrite
empty fields by default. Callers that want a full refresh pass
``overwrite=True``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from saebooks.config import Settings
from saebooks.models.contact import Contact
from saebooks.services.abr.client import lookup_abn_raw


@dataclass(frozen=True)
class AbrLookup:
    """Normalised ABR enrichment payload.

    All fields default to ``None`` / empty so partial records don't
    explode on the UI side. ``raw`` is the untouched ABR envelope,
    kept for debugging and so the UI can surface anything this
    parser doesn't map (e.g. ``AbnStatusEffectiveFrom``).
    """

    abn: str
    abn_status: str | None = None
    acn: str | None = None
    entity_name: str | None = None
    entity_type_code: str | None = None
    entity_type_name: str | None = None
    business_names: tuple[str, ...] = ()
    trading_names: tuple[str, ...] = ()
    address_state: str | None = None
    address_postcode: str | None = None
    gst_registered: bool = False
    gst_from: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def preferred_name(self) -> str | None:
        """Best display name: first trading/business name falls back to entity."""
        if self.trading_names:
            return self.trading_names[0]
        if self.business_names:
            return self.business_names[0]
        return self.entity_name


def _as_list(value: Any) -> list[str]:
    """ABR returns BusinessName/TradingName as either [], str, or list[str]."""
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


def parse_abr_response(payload: dict[str, Any]) -> AbrLookup:
    """Parse a raw ABR envelope into :class:`AbrLookup`."""
    gst_from = payload.get("Gst") or None
    return AbrLookup(
        abn=str(payload.get("Abn", "")),
        abn_status=payload.get("AbnStatus") or None,
        acn=payload.get("Acn") or None,
        entity_name=payload.get("EntityName") or None,
        entity_type_code=payload.get("EntityTypeCode") or None,
        entity_type_name=payload.get("EntityTypeName") or None,
        business_names=tuple(_as_list(payload.get("BusinessName"))),
        trading_names=tuple(_as_list(payload.get("TradingName"))),
        address_state=payload.get("AddressState") or None,
        address_postcode=payload.get("AddressPostcode") or None,
        # ABR emits a date string (first GST-registration date) rather
        # than a boolean. Presence == currently registered.
        gst_registered=bool(gst_from),
        gst_from=gst_from,
        raw=payload,
    )


async def lookup_abn(
    abn: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> AbrLookup:
    """Fetch + parse in one call. Preferred public entry point."""
    raw = await lookup_abn_raw(abn, settings=settings, client=client)
    return parse_abr_response(raw)


def apply_to_contact(
    contact: Contact,
    lookup: AbrLookup,
    *,
    overwrite: bool = False,
) -> list[str]:
    """Merge ``lookup`` into ``contact``. Returns the list of fields changed.

    Only fields the Contact model actually has are touched. Extra ABR
    data (entity type, trading names) is surfaced via the UI but not
    persisted — adding those columns is a separate migration step.
    """
    changed: list[str] = []

    def set_if_empty(attr: str, value: str | None) -> None:
        if value is None or value == "":
            return
        current = getattr(contact, attr, None)
        if (overwrite or not current) and current != value:
            setattr(contact, attr, value)
            changed.append(attr)

    # ABN normalises to the canonical spaced form.
    if lookup.abn and (overwrite or not contact.abn):
        formatted = _format_abn(lookup.abn)
        if contact.abn != formatted:
            contact.abn = formatted
            changed.append("abn")

    set_if_empty("name", lookup.preferred_name)
    set_if_empty("state", lookup.address_state)
    set_if_empty("postcode", lookup.address_postcode)

    return changed


def _format_abn(raw: str) -> str:
    """ '87744586592' -> '87 744 586 592' (ABR canonical form)."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 11:
        return raw
    return f"{digits[0:2]} {digits[2:5]} {digits[5:8]} {digits[8:11]}"
