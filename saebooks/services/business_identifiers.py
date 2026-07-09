"""Business-identifier service — per-jurisdiction company IDs.

Generalises the legacy ``Company.abn`` column to a child table keyed
by ``(company_id, scheme)`` where ``scheme`` is one of the values
in ``KNOWN_SCHEMES`` (extensible — adding a new jurisdiction means
appending here, not altering an enum).

The legacy ``Company.abn`` column is still authoritative for callers
that read it directly; on write the M0 path is to use ``upsert`` here
which mirrors to the child table. The migration backfills existing
``companies.abn`` rows so reads through this service are immediately
consistent on existing installs.

M1.5 · T9 additions (see docs/multi-jurisdiction.md (M1.5)):
jurisdiction / check_digit_valid / valid_from / valid_to / issuing_authority
columns, plus a per-scheme validator registry (``register_validator`` /
``validate``). A validator is a light, non-raising format-and/or-check-digit
check: it never rejects a write, it only reports what it found so callers
and reviewers can see whether an identifier looks structurally sound.
Only ``au_abn``, ``au_acn`` and ``nz_ird`` have a real checksum algorithm
registered (each verified against a published worked example); the rest
of the new schemes are format-only (regex), and schemes with no validator
at all (``nz_nzbn``, ``uk_crn``, ``ee_regcode``, ``global_lei``) are
untouched — ``validate()`` returns ``None`` for those, same as before this
change, so existing callers see no behavioural difference.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.business_identifier import BusinessIdentifier

# Registry of accepted scheme keys. Extensible — keep DB column as
# free-text String(32) so a new entry here is a code-only change.
KNOWN_SCHEMES: frozenset[str] = frozenset({
    "au_abn",
    "au_acn",
    "nz_nzbn",
    "uk_crn",
    "ee_regcode",
    "global_lei",
    # M1.5 · T9 additions.
    "us_ein",
    "uk_utr",
    "uk_vat",
    "eu_vat",
    "in_gstin",
    "in_pan",
    "nz_ird",
    "ca_bn",
})

# Best-effort scheme -> jurisdiction-code mapping (matches the reference DB's
# jurisdictions.code, e.g. 'AUS', 'GBR' — see saebooks/models/reference/
# jurisdiction.py). No FK; this is just a sensible default for new writes
# that don't specify a jurisdiction explicitly. Schemes with no single
# owning country (a global LEI, a multi-country EU VAT number) are omitted
# — callers pass jurisdiction explicitly for those if they want one stored.
_SCHEME_JURISDICTION: dict[str, str] = {
    "au_abn": "AUS",
    "au_acn": "AUS",
    "nz_nzbn": "NZL",
    "nz_ird": "NZL",
    "uk_crn": "GBR",
    "uk_utr": "GBR",
    "uk_vat": "GBR",
    "ee_regcode": "EST",
    "us_ein": "USA",
    "in_gstin": "IND",
    "in_pan": "IND",
    "ca_bn": "CAN",
}


class UnknownScheme(ValueError):
    """Raised when a caller passes a scheme outside ``KNOWN_SCHEMES``."""


def _validate_scheme(scheme: str) -> str:
    if scheme not in KNOWN_SCHEMES:
        raise UnknownScheme(
            f"Unknown business-identifier scheme {scheme!r}. "
            f"Accepted: {sorted(KNOWN_SCHEMES)}"
        )
    return scheme


def _derive_jurisdiction(scheme: str) -> str | None:
    """Best-effort default jurisdiction for a scheme, or None if the
    scheme has no single owning jurisdiction (global_lei, eu_vat)."""
    return _SCHEME_JURISDICTION.get(scheme)


# ---------------------------------------------------------------------------
# Per-scheme validation registry.
#
# A validator is ``Callable[[str], bool]`` — it takes the raw stored value
# and returns True (looks structurally valid), False (does not), and is
# never called for a scheme with no registered validator (``validate()``
# returns None in that case: "no opinion", not "invalid"). Validators never
# raise and never mutate the stored value — this stays additive: writing an
# identifier that fails validation is still permitted, the caller/reviewer
# just sees ``check_digit_valid=False`` on the row.
# ---------------------------------------------------------------------------

SchemeValidator = Callable[[str], bool]

_VALIDATORS: dict[str, SchemeValidator] = {}


def register_validator(scheme: str) -> Callable[[SchemeValidator], SchemeValidator]:
    """Decorator: register ``fn`` as the validator for ``scheme``.

    Schemes without a call to this stay unvalidated — ``validate()``
    returns ``None`` for them, same as before T9.
    """

    def _decorator(fn: SchemeValidator) -> SchemeValidator:
        _VALIDATORS[scheme] = fn
        return fn

    return _decorator


def validate(scheme: str, value: str) -> bool | None:
    """Run the registered validator for ``scheme`` against ``value``.

    Returns True/False if a validator is registered, else None (no known
    format/check-digit rule for this scheme — accepted as-is).
    """
    validator = _VALIDATORS.get(scheme)
    if validator is None:
        return None
    return validator(value)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


@register_validator("au_abn")
def _validate_au_abn(value: str) -> bool:
    """ABN checksum (ATO mod-89), e.g. 51 824 753 556.

    Weights [10,1,3,5,7,9,11,13,15,17,19] over the 11 digits with 1
    subtracted from the first digit; valid iff the weighted sum is a
    multiple of 89.
    """
    digits = _digits_only(value)
    if len(digits) != 11:
        return False
    d = [int(c) for c in digits]
    d[0] -= 1
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    total = sum(x * w for x, w in zip(d, weights, strict=True))
    return total % 89 == 0


@register_validator("au_acn")
def _validate_au_acn(value: str) -> bool:
    """ACN checksum (ASIC mod-10), e.g. 004 085 616.

    Weights [8,7,6,5,4,3,2,1] over the first 8 digits; the check digit
    is ``(10 - (sum % 10)) % 10`` and must equal the 9th digit.
    """
    digits = _digits_only(value)
    if len(digits) != 9:
        return False
    weights = (8, 7, 6, 5, 4, 3, 2, 1)
    total = sum(
        int(c) * w for c, w in zip(digits[:8], weights, strict=True)
    )
    check = (10 - (total % 10)) % 10
    return check == int(digits[8])


@register_validator("nz_ird")
def _validate_nz_ird(value: str) -> bool:
    """NZ IRD number checksum (Inland Revenue mod-11 double-weight).

    8 or 9 digit number; the base (all but the last digit) is left-padded
    to 8 digits and weighted [3,2,7,6,5,4,3,2]. If the primary check digit
    comes out to 10, a second weighting [7,4,3,2,5,2,7,6] is tried; if that
    also comes out to 10 the number is invalid. Verified against IRD's
    published example 49-091-850.
    """
    digits = _digits_only(value)
    if len(digits) not in (8, 9):
        return False
    base, check_digit = digits[:-1], int(digits[-1])
    padded = [0] * (8 - len(base)) + [int(c) for c in base]

    def _weighted(weights: tuple[int, ...]) -> int | None:
        total = sum(x * w for x, w in zip(padded, weights, strict=True))
        remainder = total % 11
        if remainder == 0:
            return 0
        candidate = 11 - remainder
        return candidate

    primary = _weighted((3, 2, 7, 6, 5, 4, 3, 2))
    if primary == 10:
        primary = _weighted((7, 4, 3, 2, 5, 2, 7, 6))
        if primary == 10:
            return False
    return primary == check_digit


def _format_validator(pattern: str) -> SchemeValidator:
    compiled = re.compile(pattern)

    def _fn(value: str) -> bool:
        return compiled.match(value.strip().upper()) is not None

    return _fn


# Format-only schemes — no checksum algorithm implemented, just a shape
# check. Structurally these are the same registration mechanism as the
# checksum validators above; a real check-digit algorithm can replace any
# of these later without touching callers.
register_validator("us_ein")(_format_validator(r"^\d{2}-?\d{7}$"))
register_validator("uk_utr")(_format_validator(r"^\d{10}$"))
register_validator("uk_vat")(_format_validator(r"^(GB)?\d{9}(\d{3})?$"))
register_validator("eu_vat")(_format_validator(r"^[A-Z]{2}[A-Z0-9]{2,12}$"))
register_validator("in_gstin")(
    _format_validator(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9]$")
)
register_validator("in_pan")(_format_validator(r"^[A-Z]{5}\d{4}[A-Z]$"))
register_validator("ca_bn")(_format_validator(r"^\d{9}(RT\d{4})?$"))


async def get(
    session: AsyncSession,
    company_id: uuid.UUID,
    scheme: str,
) -> BusinessIdentifier | None:
    """Return the identifier row for ``(company_id, scheme)`` or None."""
    _validate_scheme(scheme)
    result = await session.execute(
        select(BusinessIdentifier).where(
            BusinessIdentifier.company_id == company_id,
            BusinessIdentifier.scheme == scheme,
        )
    )
    return result.scalars().first()


async def upsert(
    session: AsyncSession,
    company_id: uuid.UUID,
    scheme: str,
    value: str,
    *,
    tenant_id: uuid.UUID | None = None,
    verified_at: datetime | None = None,
    jurisdiction: str | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    issuing_authority: str | None = None,
    auto_validate: bool = True,
) -> BusinessIdentifier:
    """Insert or update the identifier for ``(company_id, scheme)``.

    ``jurisdiction`` defaults to a scheme-derived value (see
    ``_derive_jurisdiction``) when not supplied — the model column has no
    single fixed default because the right value differs per scheme.
    When ``auto_validate`` is True (the default) and the scheme has a
    registered validator, ``check_digit_valid`` is computed from ``value``;
    schemes with no validator leave it unchanged (None on insert). Callers
    who supply their own ``verified_at`` are asserting the identifier was
    verified some other way (e.g. an external registry lookup) — that is
    independent of ``check_digit_valid``, which only reflects the local
    format/checksum check.

    Caller is responsible for committing the session.
    """
    _validate_scheme(scheme)
    resolved_jurisdiction = (
        jurisdiction if jurisdiction is not None else _derive_jurisdiction(scheme)
    )
    check_digit_valid = validate(scheme, value) if auto_validate else None

    existing = await get(session, company_id, scheme)
    if existing is not None:
        existing.value = value
        existing.updated_at = datetime.now(UTC)
        if verified_at is not None:
            existing.verified_at = verified_at
        if jurisdiction is not None:
            existing.jurisdiction = jurisdiction
        elif existing.jurisdiction is None:
            existing.jurisdiction = resolved_jurisdiction
        if auto_validate:
            existing.check_digit_valid = check_digit_valid
        if valid_from is not None:
            existing.valid_from = valid_from
        if valid_to is not None:
            existing.valid_to = valid_to
        if issuing_authority is not None:
            existing.issuing_authority = issuing_authority
        return existing

    row = BusinessIdentifier(
        company_id=company_id,
        scheme=scheme,
        value=value,
        verified_at=verified_at,
        jurisdiction=resolved_jurisdiction,
        check_digit_valid=check_digit_valid,
        valid_from=valid_from,
        valid_to=valid_to,
        issuing_authority=issuing_authority,
    )
    if tenant_id is not None:
        row.tenant_id = tenant_id
    session.add(row)
    await session.flush()
    return row
