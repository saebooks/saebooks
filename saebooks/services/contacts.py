"""Contact service — CRUD, search, archive.

All writes bump ``Contact.version`` and append a row to ``change_log``
so the Phase 0 API (and the offline-sync work in Phase 4.5) has a
single authoritative source of truth. Legacy Jinja routes call the
same helpers, which is why ``update`` and ``archive`` accept an
optional ``expected_version`` — the API passes it (enforcing
``If-Match``), and the Jinja layer omits it (last-writer-wins, same
as today).
"""
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact, ContactType, PaymentTermsBasis
from saebooks.services import audit as audit_svc
from saebooks.services import change_log as change_log_svc

# ABN: exactly 11 digits after stripping spaces
_ABN_RE = re.compile(r"^\d{11}$")


class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value.

    The API layer catches this and returns 409 with the current server
    state so the client can reconcile.
    """

    def __init__(self, current: Contact) -> None:
        super().__init__(
            f"Contact {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


def _validate_abn(raw: str) -> str:
    """Strip spaces and validate ABN is exactly 11 digits. Returns cleaned value."""
    cleaned = raw.replace(" ", "")
    if not _ABN_RE.match(cleaned):
        raise ValueError(
            f"Invalid ABN '{raw}' — must be exactly 11 digits (spaces are allowed)."
        )
    return cleaned


_CONTACT_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "name",
    "family_name",
    "given_name",
    "other_given_name",
    "contact_type",
    "email",
    "phone",
    "abn",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postcode",
    "country",
    "notes",
    "default_account_id",
    "default_tax_code",
    "currency_code",
    "bank_bsb",
    "bank_account_number",
    "bank_account_title",
    "tfn",
    "share_percentage",
    "default_income_classification",
    "created_at",
    "updated_at",
    "archived_at",
    "is_tpar_supplier",
    "payment_terms_basis",
    "payment_terms_days",
    "version",
)


def _serialise(contact: Contact) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload.

    Uses an explicit column list so we don't trigger SQLAlchemy
    inspection magic (which can bridge into greenlet IO if any
    attribute is still pending).
    """
    data: dict[str, Any] = {}
    for key in _CONTACT_COLUMNS:
        val = getattr(contact, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = str(val)
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    contact_type: ContactType | None = None,
    search: str | None = None,
    is_one_off: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Contact]:
    """List active contacts, optionally filtered by type, search term, or one-off flag.

        ``True`` to show only one-offs and ``False`` to hide them.
    """
    stmt = (
        select(Contact)
        .where(Contact.company_id == company_id, Contact.archived_at.is_(None))
    )
    if tenant_id is not None:
        stmt = stmt.where(Contact.tenant_id == tenant_id)
    if contact_type is not None:
        stmt = stmt.where(Contact.contact_type == contact_type)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            Contact.name.ilike(pattern) | Contact.email.ilike(pattern)
        )
    if is_one_off is not None:
        stmt = stmt.where(Contact.is_one_off == is_one_off)
    stmt = stmt.order_by(Contact.name).offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Contact | None:
    """Fetch a contact by id.

    When ``tenant_id`` is supplied, the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists. The
    parameter is keyword-only and optional so existing callers
    (legacy Jinja routes, services that already filtered by company)
    keep working unchanged; the API layer always supplies it.

    P0 cross-tenant leak fix: the bare ``session.get(Contact, id)`` of
    the original implementation was an unscoped PK lookup — anyone who
    learned a foreign-tenant UUID via the leaky list endpoint could
    fetch the detail. With ``tenant_id`` supplied we now reject those
    lookups defensively, on top of the FORCE-RLS gate at the DB layer.
    """
    if tenant_id is None and company_id is None:
        return await session.get(Contact, contact_id)
    clauses = [Contact.id == contact_id]
    if tenant_id is not None:
        clauses.append(Contact.tenant_id == tenant_id)
    if company_id is not None:
        clauses.append(Contact.company_id == company_id)
    result = await session.execute(
        select(Contact).where(*clauses)
    )
    return result.scalars().first()


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    actor: str = "web",
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
    name: str,
    family_name: str | None = None,
    given_name: str | None = None,
    other_given_name: str | None = None,
    contact_type: ContactType,
    email: str | None = None,
    phone: str | None = None,
    abn: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postcode: str | None = None,
    country: str = "Australia",
    notes: str | None = None,
    default_account_id: uuid.UUID | None = None,
    default_tax_code: str | None = None,
    currency_code: str | None = None,
    tfn: str | None = None,
    share_percentage: object = None,
    default_income_classification: str | None = None,
    is_tpar_supplier: bool = False,
    is_one_off: bool = False,
    payment_terms_basis: PaymentTermsBasis | None = None,
    payment_terms_days: int | None = None,
    e_invoice_recipient: bool = False,
    peppol_participant_id: str | None = None,
) -> Contact:
    """Create a new contact. Validate ABN format if provided (11 digits)."""
    if abn is not None:
        abn = _validate_abn(abn)

    contact = Contact(
        company_id=company_id,
        tenant_id=tenant_id,
        name=name.strip(),
        family_name=family_name,
        given_name=given_name,
        other_given_name=other_given_name,
        contact_type=contact_type,
        email=email,
        phone=phone,
        abn=abn,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        state=state,
        postcode=postcode,
        country=country,
        notes=notes,
        default_account_id=default_account_id,
        default_tax_code=default_tax_code,
        currency_code=currency_code,
        tfn=tfn,
        share_percentage=share_percentage,
        default_income_classification=default_income_classification,
        is_tpar_supplier=is_tpar_supplier,
        is_one_off=is_one_off,
        payment_terms_basis=payment_terms_basis,
        payment_terms_days=payment_terms_days,
        e_invoice_recipient=e_invoice_recipient,
        peppol_participant_id=peppol_participant_id,
        version=1,
    )
    session.add(contact)
    await session.flush()
    # Pull server-side defaults (created_at, updated_at) so the
    # serialised payload isn't missing fields.
    await session.refresh(contact)
    await change_log_svc.append(
        session,
        entity="contact",
        entity_id=contact.id,
        op="create",
        actor=actor,
        payload=_serialise(contact),
        version=contact.version,
        tenant_id=tenant_id,
    )
    await session.commit()
    return contact


async def update(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    actor: str | None = None,
    expected_version: int | None = None,
    tenant_id: uuid.UUID | None = None,
    **kwargs,
) -> Contact:
    """Update contact fields. Only update fields that are explicitly passed.

    If ``expected_version`` is supplied and does not match the stored
    ``Contact.version``, raises ``VersionConflict``. Otherwise bumps
    the version, appends a change_log row, and commits.

    When ``tenant_id`` is supplied, a foreign-tenant id raises
    ``ValueError`` (treated as not found) — cross-tenant probes 404.
    """
    contact = await get(session, contact_id, tenant_id=tenant_id)
    if contact is None:
        raise ValueError(f"Contact {contact_id} not found")

    if expected_version is not None and contact.version != expected_version:
        raise VersionConflict(contact)

    if "abn" in kwargs and kwargs["abn"] is not None:
        kwargs["abn"] = _validate_abn(kwargs["abn"])

    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    allowed = {
        "name", "contact_type", "email", "phone", "abn",
        "address_line1", "address_line2", "city", "state", "postcode",
        "country", "notes", "default_account_id", "default_tax_code",
        "currency_code",
        "tfn", "share_percentage", "default_income_classification",
        "is_tpar_supplier", "is_one_off",
        "payment_terms_basis", "payment_terms_days",
        "e_invoice_recipient", "peppol_participant_id", }

    before = audit_svc.capture(contact)
    for key, value in kwargs.items():
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        setattr(contact, key, value)

    contact.version = contact.version + 1

    await audit_svc.snapshot_row(
        session, contact,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    # snapshot_row flushed; refresh to pull the new onupdate=func.now()
    # timestamp before we serialise for change_log.
    await session.refresh(contact)
    await change_log_svc.append(
        session,
        entity="contact",
        entity_id=contact.id,
        op="update",
        actor=actor or performed_by or "web",
        payload=_serialise(contact),
        version=contact.version,
        tenant_id=contact.tenant_id,
    )
    await session.commit()
    return contact


async def archive(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    actor: str | None = None,
    expected_version: int | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Contact | None:
    """Soft-delete.

    When ``tenant_id`` is supplied, a foreign-tenant id returns ``None``
    silently — cross-tenant archive is a no-op.
    """
    contact = await get(session, contact_id, tenant_id=tenant_id)
    if contact is None:
        return None
    if expected_version is not None and contact.version != expected_version:
        raise VersionConflict(contact)
    before = audit_svc.capture(contact)
    contact.archived_at = datetime.now(UTC)
    contact.version = contact.version + 1
    await audit_svc.snapshot_row(
        session, contact,
        action="archive",
        before_data=before,
        performed_by=performed_by,
    )
    await session.refresh(contact)
    await change_log_svc.append(
        session,
        entity="contact",
        entity_id=contact.id,
        op="archive",
        actor=actor or performed_by or "web",
        payload=_serialise(contact),
        version=contact.version,
        tenant_id=contact.tenant_id,
    )
    await session.commit()
    return contact


async def search_by_name(
    session: AsyncSession,
    company_id: uuid.UUID,
    query: str,
    limit: int = 10,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[Contact]:
    """Quick search for autocomplete — ILIKE on name.

    P0 defence-in-depth: when ``tenant_id`` is supplied the result is
    additionally filtered by tenant so a corrupt row cannot surface to
    the wrong tenant via the autocomplete path.
    """
    stmt = (
        select(Contact)
        .where(
            Contact.company_id == company_id,
            Contact.archived_at.is_(None),
            Contact.name.ilike(f"%{query}%"),
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(Contact.tenant_id == tenant_id)
    stmt = stmt.order_by(Contact.name).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
