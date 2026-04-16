"""Contact service — CRUD, search, archive."""
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact, ContactType
from saebooks.services import audit as audit_svc

# ABN: exactly 11 digits after stripping spaces
_ABN_RE = re.compile(r"^\d{11}$")


def _validate_abn(raw: str) -> str:
    """Strip spaces and validate ABN is exactly 11 digits. Returns cleaned value."""
    cleaned = raw.replace(" ", "")
    if not _ABN_RE.match(cleaned):
        raise ValueError(
            f"Invalid ABN '{raw}' — must be exactly 11 digits (spaces are allowed)."
        )
    return cleaned


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    contact_type: ContactType | None = None,
    search: str | None = None,
    limit: int = 200,
) -> list[Contact]:
    """List active contacts, optionally filtered by type or search term (name/email)."""
    stmt = (
        select(Contact)
        .where(Contact.company_id == company_id, Contact.archived_at.is_(None))
    )
    if contact_type is not None:
        stmt = stmt.where(Contact.contact_type == contact_type)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            Contact.name.ilike(pattern) | Contact.email.ilike(pattern)
        )
    stmt = stmt.order_by(Contact.name).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, contact_id: uuid.UUID) -> Contact | None:
    return await session.get(Contact, contact_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str,
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
) -> Contact:
    """Create a new contact. Validate ABN format if provided (11 digits)."""
    if abn is not None:
        abn = _validate_abn(abn)

    contact = Contact(
        company_id=company_id,
        name=name.strip(),
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
    )
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact


async def update(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    **kwargs,
) -> Contact:
    """Update contact fields. Only update fields that are explicitly passed."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise ValueError(f"Contact {contact_id} not found")

    if "abn" in kwargs and kwargs["abn"] is not None:
        kwargs["abn"] = _validate_abn(kwargs["abn"])

    if "name" in kwargs and kwargs["name"] is not None:
        kwargs["name"] = kwargs["name"].strip()

    allowed = {
        "name", "contact_type", "email", "phone", "abn",
        "address_line1", "address_line2", "city", "state", "postcode",
        "country", "notes", "default_account_id", "default_tax_code",
    }

    before = audit_svc.capture(contact)
    for key, value in kwargs.items():
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        setattr(contact, key, value)

    await audit_svc.snapshot_row(
        session, contact,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    await session.refresh(contact)
    return contact


async def archive(
    session: AsyncSession,
    contact_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    """Soft-delete."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        return
    before = audit_svc.capture(contact)
    contact.archived_at = datetime.now(UTC)
    await audit_svc.snapshot_row(
        session, contact,
        action="archive",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()


async def search_by_name(
    session: AsyncSession,
    company_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[Contact]:
    """Quick search for autocomplete — ILIKE on name."""
    result = await session.execute(
        select(Contact)
        .where(
            Contact.company_id == company_id,
            Contact.archived_at.is_(None),
            Contact.name.ilike(f"%{query}%"),
        )
        .order_by(Contact.name)
        .limit(limit)
    )
    return list(result.scalars().all())
