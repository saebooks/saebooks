"""AU superannuation-vehicle validation rules.

Jurisdiction-module architecture Phase 1 (design doc §5.2): the AU
vehicle rules extracted from ``services/super_funds.py`` —

* APRA-regulated funds carry a **USI** (Unique Superannuation
  Identifier, exactly 11 chars).
* SMSFs carry **ABN + ESA** (electronic service address; bank details
  ride separately, encrypted, on the row).

The jurisdiction-neutral parts stay in ``services/super_funds.py``:
the CRUD, the "exactly one default per company" invariant, and the
SMSF bank-field encryption. Error type + codes are unchanged
(``SuperFundError``, ``smsf_missing_fields`` / ``apra_missing_usi`` /
``usi_bad_length``) — the API surface sees no difference.

Neutral-core strip (Job D): this function is registered into
``services.super_funds``'s per-jurisdiction validator registry from
``jurisdictions/au/__init__.py`` (``_au_validate_retirement_account``)
rather than imported directly by ``services/super_funds.py`` — the
core dispatches on ``Company.jurisdiction`` and never names this
module.
"""
from __future__ import annotations

from saebooks.services.super_funds import SuperFundError


def validate_fund_fields(
    *,
    is_smsf: bool,
    usi: str | None,
    employer_abn: str | None,
    esa: str | None,
) -> None:
    """Validate an AU super fund's vehicle-specific fields.

    Raises :class:`SuperFundError` early so the DB CHECK constraint is
    never the first error the caller sees.
    """
    if is_smsf:
        if not (employer_abn and esa):
            raise SuperFundError(
                "SMSF requires employer_abn + esa", code="smsf_missing_fields"
            )
    else:
        if not usi:
            raise SuperFundError(
                "APRA-regulated fund requires usi", code="apra_missing_usi"
            )
        if len(usi) != 11:
            raise SuperFundError(
                f"USI must be exactly 11 chars (got {len(usi)})", code="usi_bad_length"
            )


__all__ = ["validate_fund_fields"]
