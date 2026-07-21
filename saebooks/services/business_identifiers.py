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

M1.5 · T9 additions (see ~/records/saebooks/global-reference-audit-2026-07-09.md):
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
from typing import Any

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
    # EE VAT number ("käibemaksukohustuslase number" / KMV). Follows the
    # ``uk_vat`` / ``lt_vat`` / ``eu_vat`` scheme-naming convention (the
    # generic ``_vat`` suffix, not the local KMV term — the same generic
    # posture as ``ee_regcode`` over "registrikood"). Format validator
    # registers lazily from ``saebooks.jurisdictions.ee.identifiers`` on
    # first EE onboarding dispatch (the ``lv_pvn`` precedent).
    "ee_vat",
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
    # LT jurisdiction module. Validators register lazily from
    # saebooks.jurisdictions.lt.identifiers on first LT dispatch (the
    # nz_nzbn precedent) — until then validate() returns None for these.
    "lt_company_code",
    "lt_vat",
    "lt_personal_code",
    # LV jurisdiction module (validators registered lazily by
    # saebooks.jurisdictions.lv.identifiers on first LV dispatch).
    "lv_regnum",
    "lv_pvn",
    # M1.5 P1 tail — customs/trade identifier + EU cross-border VAT
    # simplification scheme-membership records. Format-only validators
    # below (no checksum algorithm), same posture as us_ein/uk_utr/eu_vat.
    "eori",  # Economic Operators Registration and Identification (EU customs)
    "eu_oss_scheme",  # One-Stop-Shop union/non-union scheme membership number
    "eu_ioss_scheme",  # Import One-Stop-Shop scheme membership number
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
    "ee_vat": "EST",
    "us_ein": "USA",
    "in_gstin": "IND",
    "in_pan": "IND",
    "ca_bn": "CAN",
    "lt_company_code": "LTU",
    "lt_vat": "LTU",
    "lt_personal_code": "LTU",
    "lv_regnum": "LVA",
    "lv_pvn": "LVA",
}


class UnknownScheme(ValueError):
    """Raised when a caller passes a scheme outside ``KNOWN_SCHEMES``."""


class DuplicateIdentifier(Exception):
    """Raised by ``upsert`` when writing a value that another company in
    the same tenant already holds under a value-unique scheme.

    Value-uniqueness is deliberately per-scheme (see
    ``_VALUE_UNIQUE_SCHEMES``) AND opt-in (``upsert(..., enforce_unique=
    True)``), NOT a blanket rule: a company registration number that legally
    identifies exactly one entity (the Estonian registrikood / KMV number)
    must be unique per tenant when written through the company service,
    whereas ``au_abn`` / ``au_acn`` stay value-unconstrained — matching what
    ``business_identifiers`` enforces at the table level (only ``(company_id,
    scheme)`` uniqueness). The API layer maps this to HTTP 409.
    """

    def __init__(self, scheme: str, value: str) -> None:
        self.scheme = scheme
        self.value = value
        super().__init__(
            f"Another company already holds {value!r} under scheme {scheme!r}."
        )


# Schemes whose ``value`` must be unique per tenant — a registry code that
# legally names exactly one entity. Enforced only when a caller passes
# ``enforce_unique=True`` to ``upsert`` (the company write path does); there
# is NO DB unique index, because the e-invoice/lodgement test fixtures
# legitimately reuse a single registrikood across many scratch companies in
# one tenant and an index would reject them regardless of code path. Extend
# deliberately — au_abn/au_acn are NOT here (unchanged unconstrained
# behaviour).
_VALUE_UNIQUE_SCHEMES: frozenset[str] = frozenset({"ee_regcode", "ee_vat"})


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


# The one "primary" registry-code scheme per jurisdiction — the identifier a
# jurisdiction stamps on invoices / lodgements as *the* company registration
# number. Keyed by ``Company.jurisdiction`` (2- or 3-char, e.g. 'AU'/'AUS').
# This is what de-overloads the legacy ``Company.abn`` column, which stored an
# ABN for AU companies but the äriregistri kood for EE companies: each now
# lands in its correctly-typed scheme, and a caller asks for "the company's
# primary registration number" jurisdiction-aware, rather than reading a
# column that meant different things per country.
_PRIMARY_SCHEME_BY_JURISDICTION: dict[str, str] = {
    "AU": "au_abn",
    "AUS": "au_abn",
    "EE": "ee_regcode",
    "EST": "ee_regcode",
}


def primary_scheme_for(jurisdiction: str | None) -> str | None:
    """The primary registry-code scheme for a ``Company.jurisdiction`` value,
    or None if the jurisdiction has no single mapped scheme."""
    if not jurisdiction:
        return None
    return _PRIMARY_SCHEME_BY_JURISDICTION.get(jurisdiction.strip().upper())


async def primary_registry_identifier(session: AsyncSession, company: Any) -> str:
    """Resolve a company's primary registration number (jurisdiction-aware).

    AU company -> its ``au_abn`` value; EE company -> its ``ee_regcode``
    value; etc. Returns "" when the jurisdiction has no mapped scheme or the
    company has not recorded that identifier. Callers who want *specifically*
    an ABN read ``company.abn`` (the AU-only hybrid); callers who want *the*
    registration number whatever the country (e-invoicing seller identity)
    use this."""
    scheme = primary_scheme_for(getattr(company, "jurisdiction", None))
    if scheme is None:
        return ""
    row = await get(session, company.id, scheme)
    return (row.value if row is not None else "") or ""


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
# EORI: 2-letter ISO country code + up to 15 alphanumeric characters.
register_validator("eori")(_format_validator(r"^[A-Z]{2}[A-Z0-9]{1,15}$"))
# OSS/IOSS scheme-membership numbers. IOSS is a fixed shape (IM + 10
# digits); OSS union-scheme numbers reuse the identification member
# state's VAT-number shape (EU country code + 2-12 alphanumerics) — same
# pattern as eu_vat above.
register_validator("eu_ioss_scheme")(_format_validator(r"^IM\d{10}$"))
register_validator("eu_oss_scheme")(_format_validator(r"^[A-Z]{2}[A-Z0-9]{2,12}$"))


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


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _guard_value_unique(
    session: AsyncSession,
    company_id: uuid.UUID,
    scheme: str,
    value: str,
    tenant_id: uuid.UUID | None,
) -> None:
    """Raise ``DuplicateIdentifier`` if another company in the same tenant
    already holds ``value`` under a value-unique ``scheme``.

    Runs BEFORE any INSERT/UPDATE so the (sequential) duplicate path never
    poisons the session. No-op for schemes outside ``_VALUE_UNIQUE_SCHEMES``
    (au_abn/au_acn stay unconstrained). Only invoked when the caller passes
    ``enforce_unique=True`` (the company write path) — see ``upsert``.
    """
    if scheme not in _VALUE_UNIQUE_SCHEMES:
        return
    tid = tenant_id if tenant_id is not None else _DEFAULT_TENANT_ID
    clash = await session.execute(
        select(BusinessIdentifier.id).where(
            BusinessIdentifier.tenant_id == tid,
            BusinessIdentifier.scheme == scheme,
            BusinessIdentifier.value == value,
            BusinessIdentifier.company_id != company_id,
        )
    )
    if clash.scalars().first() is not None:
        raise DuplicateIdentifier(scheme, value)


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
    enforce_unique: bool = False,
) -> BusinessIdentifier:
    """Insert or update the identifier for ``(company_id, scheme)``.

    ``enforce_unique`` (default False) opts the write into per-tenant
    VALUE uniqueness for a value-unique scheme (``_VALUE_UNIQUE_SCHEMES``,
    currently the EE registry codes): if another company in the tenant
    already holds this value, raise ``DuplicateIdentifier``. The default is
    False so this stays a bare primitive matching what the table enforces
    (``(company_id, scheme)`` only) — direct callers (e-invoice / lodgement
    test fixtures that legitimately reuse a registrikood across scratch
    companies, external-registry sync) are unaffected. The sanctioned
    company write path (``services.companies._set_company_registry_id``)
    passes ``enforce_unique=True`` so registrikood/KMV duplicates are a 409
    at create/update. There is intentionally NO DB unique index (that would
    reject the direct-caller fixtures regardless of path); the pre-check
    covers the sequential/retry case, and a truly-concurrent duplicate
    create is a rare, recoverable edge left out of scope.

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
    if enforce_unique:
        await _guard_value_unique(session, company_id, scheme, value, tenant_id)
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
