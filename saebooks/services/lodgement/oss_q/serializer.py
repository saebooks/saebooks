"""OSS-Q — persistence (real, built) + wire-format serializer (STOP).

⛔ STOP-AND-CONFIRM — the wire format is NOT built here
---------------------------------------------------------
The Union OSS return is filed through EMTA's OSS portal in a **Commission-
defined, EU-harmonised XML schema** (the OSS/IOSS Council Implementing
Regulation schema) — it is genuinely absent from the e-MTA national
KMD/TSD package (grepped ``emta-schemas/all-links.txt`` + ``README.md``:
no OSS/MOSS artefact; the in-tree ``CESOP-XSD-...zip`` is payment-service-
provider reporting, a different regime — do not reuse it for OSS). Per
the build task's explicit instruction, this module does NOT invent or
guess that schema's element/namespace/root names. ``build_oss_q_xml_document``
below is a clearly-marked ``NotImplementedError`` stub, not a
self-authored placeholder XML — inventing plausible-looking government
XML would be worse than refusing, since a filer could mistake it for
something real. See ``~/records/saebooks/ee-frontier-build-plan.md``
§"MODULE 2" (top-5 risk #2) for the full disposition, and the OPEN
QUESTION this build's report surfaces for Richard: source the real
Commission OSS Union-scheme XSD before this stub can be filled in.

What IS built: ``persist_oss_return`` below persists a computed
``OssQListing`` (``generator.py``) to ``tax_returns`` in SAE Books' OWN
JSONB shape — this is NOT the wire format, it is the same
"engine-internal computed-return record" every other lodgement module
already writes (mirrors ``tsd.serializer.persist_tsd_return`` — list-
shaped ``figures``, since OSS-Q, like TSD, is a repeating-row listing
not a fixed box vector — see ``generator.py``'s module docstring).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.tax_return import TaxReturn, TaxReturnStatus
from saebooks.services.lodgement.oss_q.generator import OssQListing


class OssQWireFormatNotImplemented(NotImplementedError):
    """Raised by ``build_oss_q_xml_document`` — the real Commission OSS
    Union-scheme XSD is not in this tree. See module docstring."""


def build_oss_q_xml_document(listing: OssQListing) -> bytes:
    """PLACEHOLDER — deliberately not implemented.

    Do NOT add a self-authored element/namespace scheme here. The real
    wire format is the EU Commission's OSS/IOSS Union-scheme XML
    (transmitted via EMTA's OSS portal, a separate rail from X-Road
    KMD3) and is not available in this repository's e-MTA schema
    package. Raises unconditionally until that schema is sourced and
    pinned — see this module's docstring."""
    raise OssQWireFormatNotImplemented(
        "OSS-Q wire-format XML is not implemented: the Commission "
        "OSS/IOSS Union-scheme XSD is not in-tree (it is EU-harmonised, "
        "not part of EMTA's national KMD/TSD schema package). Source and "
        "pin that schema before implementing this serializer — do not "
        "guess the element/namespace names. See "
        "services.lodgement.oss_q.serializer's module docstring and "
        "~/records/saebooks/ee-frontier-build-plan.md Module 2 §2.5."
    )


def _to_jsonable(value: Any) -> Any:
    """Same convention as ``tsd.serializer._to_jsonable`` — Decimal/date/
    UUID -> string, JSONB-safe."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _asdict_jsonable(obj: Any) -> dict[str, Any]:
    return {k: _to_jsonable(v) for k, v in asdict(obj).items()}


async def persist_oss_return(
    session: AsyncSession,
    listing: OssQListing,
    *,
    tenant_id: uuid.UUID,
    period_id: uuid.UUID,
    status: TaxReturnStatus = TaxReturnStatus.READY,
    generated_by_user_id: uuid.UUID | None = None,
) -> TaxReturn:
    """Persist a computed ``OssQListing`` to ``tax_returns`` (company DB),
    ``return_type="OSS-Q"``.

    List-shaped ``figures`` (mirrors ``tsd.serializer.persist_tsd_return``
    — OSS-Q is a repeating member-state x rate listing, not a box
    vector, so ``tax_return_generator.persist_return``'s flat
    ``box_code -> {amount,...}`` shape does not fit):
    ``{"cells": [...one dict per OssQCell...], "errors": [...surfaced
    data-quality errors...], "total_taxable_base": "...", "total_vat_payable": "..."}``.

    Does not commit — caller controls the transaction boundary (mirrors
    ``persist_tsd_return``/``persist_return``).
    """
    figures: dict[str, Any] = {
        "cells": [_asdict_jsonable(cell) for cell in listing.cells],
        "errors": [_asdict_jsonable(err) for err in listing.errors],
        "total_taxable_base": str(listing.total_taxable_base()),
        "total_vat_payable": str(listing.total_vat_payable()),
    }
    row = TaxReturn(
        company_id=listing.company_id,
        tenant_id=tenant_id,
        jurisdiction="EE",
        period_id=period_id,
        return_type="OSS-Q",
        figures=figures,
        status=status,
        generated_by_user_id=generated_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row
