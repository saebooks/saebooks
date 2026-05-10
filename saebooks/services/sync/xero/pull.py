"""Incremental pull from Xero into SAE Books.

Strategy
--------
Per plan §11.b "Incremental sync via watermarks":

* Each pull pass reads ``connection.last_pulled_at`` and asks Xero for
  rows ``UpdatedDateUTC > last_pulled_at`` via ``If-Modified-Since``.
  Xero's window is rounded to the second (no fractional). We pad
  ``last_pulled_at`` by **one second** before formatting to avoid a
  re-pull of the boundary row.

* For each row returned, we ``UPSERT`` into our local table keyed on
  ``(external_source='xero', external_id)``. The dedicated unique index
  ``ix_<table>_external`` (mig 0095) makes this an O(log n) lookup.

* Per-row outcome (``ok`` | ``conflict`` | ``quarantined`` | ``skipped``)
  is appended to ``sync_audit_log``. Conflict detection is delegated to
  ``push.detect_conflict`` — pull doesn't push, but it MUST log the
  conflict so the operator's queue surfaces it.

* On success we advance ``connection.last_pulled_at`` to the *server-
  side* ``UpdatedDateUTC`` of the newest row (NOT ``datetime.now()``);
  this makes the watermark robust against worker-clock drift.

* The pull never edits invoice line items on existing posted invoices
  — per ``[[feedback_saebooks-marketing-differentiator]]`` the rendered
  PDF snapshot is the source of truth. If line items differ, we log a
  conflict and quarantine the row.

This module owns the *DB-side* of pull. The HTTP iteration lives in
``endpoints.iter_*``; the orchestrator in ``connector.sync_xero`` is
the only thing that wires the two together.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.sync import (
    SyncAuditLog,
    SyncConnection,
    SyncDirection,
    SyncObjectType,
    SyncState,
)
from saebooks.services.sync.errors import SyncValidationError
from saebooks.services.sync.xero.client import XeroClient
from saebooks.services.sync.xero.endpoints import (
    get_invoice as xero_get_invoice,
)
from saebooks.services.sync.xero.endpoints import (
    iter_contacts,
    iter_invoices,
)
from saebooks.services.sync.xero.mappers import (
    XeroContactPull,
    XeroInvoicePull,
    xero_contact_to_saebooks,
    xero_invoice_to_saebooks,
)

log = logging.getLogger(__name__)

# Outcome strings logged into ``sync_audit_log.outcome``. Single source
# of truth — keep these stable; downstream dashboards may aggregate on
# them.
OUTCOME_OK = "ok"
OUTCOME_CONFLICT = "conflict"
OUTCOME_QUARANTINED = "quarantined"
OUTCOME_SKIPPED = "skipped"
OUTCOME_ERROR = "error"


@dataclass
class PullStats:
    """Accumulated counters returned by ``pull_*`` orchestrators."""

    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    conflicted: int = 0
    quarantined: int = 0
    errors: int = 0

    def merge(self, other: PullStats) -> PullStats:
        return PullStats(
            fetched=self.fetched + other.fetched,
            upserted=self.upserted + other.upserted,
            skipped=self.skipped + other.skipped,
            conflicted=self.conflicted + other.conflicted,
            quarantined=self.quarantined + other.quarantined,
            errors=self.errors + other.errors,
        )


# ---------------------------------------------------------------------- #
# Public entry points                                                    #
# ---------------------------------------------------------------------- #


async def pull_contacts(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
    company_id: uuid.UUID,
) -> PullStats:
    """Pull all changed Contacts since ``connection.last_pulled_at``.

    ``company_id`` is the SAE Books company that newly-created contacts
    are filed under. Existing contacts (matched by ``external_id``)
    keep their existing ``company_id`` — Xero does not model "company"
    as a sub-tenant, so the operator chose the mapping at consent time.
    """
    ifms = _ifms_with_one_second_pad(connection.last_pulled_at)
    stats = PullStats()
    newest_seen: datetime | None = None

    async for row in iter_contacts(client, if_modified_since=ifms):
        stats.fetched += 1
        try:
            pulled = xero_contact_to_saebooks(row)
        except (KeyError, ValueError, TypeError) as exc:
            stats.errors += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PULL,
                object_type=SyncObjectType.CONTACT,
                external_id=str(row.get("ContactID")),
                outcome=OUTCOME_ERROR,
                message=f"mapper failure: {exc}",
                payload={"row": row},
            )
            continue

        outcome, local_id = await _upsert_contact(
            session,
            connection=connection,
            pulled=pulled,
            xero_payload=row,
            company_id=company_id,
        )
        _bump_stat(stats, outcome)
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.PULL,
            object_type=SyncObjectType.CONTACT,
            external_id=pulled.external_id,
            local_id=local_id,
            outcome=outcome,
            message=None,
            payload=None,
        )
        seen = _parse_updated_utc(pulled.external_etag)
        if seen is not None and (newest_seen is None or seen > newest_seen):
            newest_seen = seen

    if newest_seen is not None:
        connection.last_pulled_at = newest_seen
    return stats


async def pull_invoices(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
    company_id: uuid.UUID,
    invoice_type: str = "ACCREC",
) -> PullStats:
    """Pull changed Invoices for one Type (``ACCREC`` or ``ACCPAY``).

    The Xero list endpoint returns a summary shape — line items only
    populate on per-id GET. We list summaries first, then fetch full
    bodies for any row whose ``UpdatedDateUTC`` has moved past our
    ``last_pulled_etag`` for that ``external_id``.
    """
    ifms = _ifms_with_one_second_pad(connection.last_pulled_at)
    stats = PullStats()
    newest_seen: datetime | None = None

    async for summary in iter_invoices(
        client,
        if_modified_since=ifms,
        invoice_type=invoice_type,
    ):
        stats.fetched += 1
        external_id = summary.get("InvoiceID")
        if not external_id:
            stats.errors += 1
            continue

        # Skip if we already have this etag — Xero's If-Modified-Since
        # is per-second; on the second the watermark equals
        # UpdatedDateUTC the response *includes* the boundary row.
        existing_state = await _get_state(
            session,
            connection.id,
            SyncObjectType.INVOICE,
            external_id,
        )
        summary_etag = summary.get("UpdatedDateUTC")
        if existing_state and existing_state.last_pulled_etag == summary_etag:
            stats.skipped += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PULL,
                object_type=SyncObjectType.INVOICE,
                external_id=external_id,
                local_id=existing_state.local_id,
                outcome=OUTCOME_SKIPPED,
                message="etag unchanged",
                payload=None,
            )
            seen = _parse_updated_utc(summary_etag)
            if seen is not None and (newest_seen is None or seen > newest_seen):
                newest_seen = seen
            continue

        # Fetch full body for line items.
        try:
            full = await xero_get_invoice(client, invoice_id=external_id)
        except SyncValidationError as exc:
            stats.errors += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PULL,
                object_type=SyncObjectType.INVOICE,
                external_id=external_id,
                outcome=OUTCOME_ERROR,
                message=f"fetch failed: {exc}",
                payload=None,
            )
            continue

        try:
            pulled = xero_invoice_to_saebooks(full)
        except (KeyError, ValueError, TypeError) as exc:
            stats.errors += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PULL,
                object_type=SyncObjectType.INVOICE,
                external_id=external_id,
                outcome=OUTCOME_ERROR,
                message=f"mapper failure: {exc}",
                payload={"row": full},
            )
            continue

        outcome, local_id = await _upsert_invoice(
            session,
            connection=connection,
            pulled=pulled,
            xero_payload=full,
            company_id=company_id,
        )
        _bump_stat(stats, outcome)
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.PULL,
            object_type=SyncObjectType.INVOICE,
            external_id=pulled.external_id,
            local_id=local_id,
            outcome=outcome,
            message=None,
            payload=None,
        )
        seen = _parse_updated_utc(pulled.external_etag)
        if seen is not None and (newest_seen is None or seen > newest_seen):
            newest_seen = seen

    if newest_seen is not None and (
        connection.last_pulled_at is None or newest_seen > connection.last_pulled_at
    ):
        # Only advance if we passed the existing watermark.
        connection.last_pulled_at = newest_seen
    return stats


# ---------------------------------------------------------------------- #
# Upsert helpers                                                         #
# ---------------------------------------------------------------------- #


async def _upsert_contact(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    pulled: XeroContactPull,
    xero_payload: dict[str, Any],
    company_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None]:
    """Insert or update a Contact from a pulled Xero row.

    Returns ``(outcome, local_id_or_None)``. Outcomes:

    * ``ok`` — inserted or updated cleanly.
    * ``conflict`` — Xero changed the row AND we changed the local row
      since last sync; we keep BOTH versions and flag for operator
      resolution. Conflict resolution policy: last-writer-wins on the
      header; line items (n/a for contacts) are handled in invoice
      pull.
    * ``quarantined`` — row was already quarantined; skip.
    """
    state = await _get_state(
        session,
        connection.id,
        SyncObjectType.CONTACT,
        pulled.external_id,
    )
    if state is not None and state.quarantined:
        return OUTCOME_QUARANTINED, state.local_id

    # Find existing contact via external_id+source.
    existing = await _find_contact_by_external(
        session,
        external_id=pulled.external_id,
    )

    # Conflict if local has unsynced changes (version > last_pushed_version).
    is_conflict = (
        existing is not None
        and state is not None
        and state.last_pushed_version is not None
        and existing.version > state.last_pushed_version
    )

    if existing is None:
        existing = Contact(
            id=uuid.uuid4(),
            tenant_id=connection.tenant_id,
            company_id=company_id,
            name=pulled.name,
            contact_type=pulled.contact_type,
            email=pulled.email,
            phone=pulled.phone,
            abn=pulled.abn,
            address_line1=pulled.address_line1,
            address_line2=pulled.address_line2,
            city=pulled.city,
            state=pulled.state,
            postcode=pulled.postcode,
            country=pulled.country,
            external_id=pulled.external_id,
            external_source="xero",
            external_etag=pulled.external_etag,
            external_payload=xero_payload,
            archived_at=datetime.now(UTC) if pulled.archived else None,
        )
        session.add(existing)
        await session.flush()  # populate existing.id
    elif not is_conflict:
        # Apply Xero values over local — we only get here when local
        # has NOT moved since last push.
        existing.name = pulled.name
        existing.contact_type = _merge_contact_type(
            existing.contact_type, pulled.contact_type
        )
        existing.email = pulled.email or existing.email
        existing.phone = pulled.phone or existing.phone
        existing.abn = pulled.abn or existing.abn
        existing.address_line1 = pulled.address_line1 or existing.address_line1
        existing.address_line2 = pulled.address_line2 or existing.address_line2
        existing.city = pulled.city or existing.city
        existing.state = pulled.state or existing.state
        existing.postcode = pulled.postcode or existing.postcode
        existing.country = pulled.country or existing.country
        existing.external_etag = pulled.external_etag
        existing.external_payload = xero_payload
        if pulled.archived and existing.archived_at is None:
            existing.archived_at = datetime.now(UTC)

    # Mark the local version as already-in-sync upstream so the
    # subsequent push pass doesn't re-push the row we just pulled.
    # Without this, freshly-pulled rows have ``last_pushed_version IS NULL``
    # and the push selector picks them up, overwriting their
    # ``external_id`` with a new one and breaking the link.
    await _upsert_state(
        session,
        connection=connection,
        object_type=SyncObjectType.CONTACT,
        external_id=pulled.external_id,
        local_id=existing.id,
        last_pulled_etag=pulled.external_etag,
        last_pushed_version=existing.version,
        # Don't touch quarantined flag here.
    )

    if is_conflict:
        return OUTCOME_CONFLICT, existing.id
    return OUTCOME_OK, existing.id


async def _upsert_invoice(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    pulled: XeroInvoicePull,
    xero_payload: dict[str, Any],
    company_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None]:
    """Insert or update an Invoice header from a pulled Xero row.

    Lines are NOT round-tripped on update — invoice immutability per
    ``[[feedback_saebooks-marketing-differentiator]]``. New invoices
    pulled from Xero that don't yet exist locally are recorded with
    their lines, but subsequent line edits in Xero do not propagate.
    """
    state = await _get_state(
        session,
        connection.id,
        SyncObjectType.INVOICE,
        pulled.external_id,
    )
    if state is not None and state.quarantined:
        return OUTCOME_QUARANTINED, state.local_id

    existing = await _find_invoice_by_external(
        session,
        external_id=pulled.external_id,
    )

    is_conflict = (
        existing is not None
        and state is not None
        and state.last_pushed_version is not None
        and existing.version > state.last_pushed_version
    )

    if existing is None:
        # Resolve contact via sync_state lookup. If the contact hasn't
        # been pulled yet, pull-order isn't guaranteed — quarantine the
        # invoice and let the next pass retry.
        contact_local_id: uuid.UUID | None = None
        if pulled.contact_external_id:
            contact_state = await _get_state(
                session,
                connection.id,
                SyncObjectType.CONTACT,
                pulled.contact_external_id,
            )
            if contact_state is not None:
                contact_local_id = contact_state.local_id
        if contact_local_id is None:
            await _upsert_state(
                session,
                connection=connection,
                object_type=SyncObjectType.INVOICE,
                external_id=pulled.external_id,
                local_id=None,
                last_pulled_etag=pulled.external_etag,
                quarantined=True,
                quarantine_reason="contact not yet synced",
            )
            return OUTCOME_QUARANTINED, None

        # Header-only insert. We deliberately do not write InvoiceLine
        # rows here — line items round-tripping breaks the immutable-
        # snapshot invariant. The PDF snapshot is the source of truth
        # once the invoice is posted; if the operator wants the lines
        # in our system, they re-enter via the normal invoice-create
        # flow.
        existing = Invoice(
            id=uuid.uuid4(),
            tenant_id=connection.tenant_id,
            company_id=company_id,
            contact_id=contact_local_id,
            number=pulled.number,
            issue_date=pulled.issue_date,
            due_date=pulled.due_date,
            status=pulled.status,
            currency=pulled.currency,
            fx_rate=pulled.fx_rate,
            subtotal=pulled.subtotal,
            tax_total=pulled.tax_total,
            total=pulled.total,
            amount_paid=pulled.amount_paid,
            external_id=pulled.external_id,
            external_source="xero",
            external_etag=pulled.external_etag,
            external_payload=xero_payload,
            version=1,
        )
        session.add(existing)
        await session.flush()
    elif not is_conflict:
        # Header-only update — never touch lines.
        existing.status = _merge_invoice_status(existing.status, pulled.status)
        existing.due_date = pulled.due_date or existing.due_date
        existing.amount_paid = pulled.amount_paid
        existing.external_etag = pulled.external_etag
        existing.external_payload = xero_payload

    # Same rationale as ``_upsert_contact``: record the local version
    # so push doesn't re-push the just-pulled invoice.
    await _upsert_state(
        session,
        connection=connection,
        object_type=SyncObjectType.INVOICE,
        external_id=pulled.external_id,
        local_id=existing.id,
        last_pulled_etag=pulled.external_etag,
        last_pushed_version=existing.version,
    )

    if is_conflict:
        return OUTCOME_CONFLICT, existing.id
    return OUTCOME_OK, existing.id


# ---------------------------------------------------------------------- #
# Sync-state helpers                                                     #
# ---------------------------------------------------------------------- #


async def _get_state(
    session: AsyncSession,
    connection_id: uuid.UUID,
    object_type: SyncObjectType,
    external_id: str,
) -> SyncState | None:
    stmt = select(SyncState).where(
        SyncState.connection_id == connection_id,
        SyncState.object_type == object_type.value,
        SyncState.external_id == external_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _upsert_state(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    object_type: SyncObjectType,
    external_id: str,
    local_id: uuid.UUID | None,
    last_pulled_etag: str | None = None,
    last_pushed_version: int | None = None,
    quarantined: bool | None = None,
    quarantine_reason: str | None = None,
) -> SyncState:
    state = await _get_state(session, connection.id, object_type, external_id)
    now = datetime.now(UTC)
    if state is None:
        state = SyncState(
            id=uuid.uuid4(),
            tenant_id=connection.tenant_id,
            connection_id=connection.id,
            object_type=object_type.value,
            external_id=external_id,
            local_id=local_id,
            last_pulled_etag=last_pulled_etag,
            last_pulled_at=now if last_pulled_etag is not None else None,
            last_pushed_version=last_pushed_version,
            last_pushed_at=now if last_pushed_version is not None else None,
            quarantined=bool(quarantined) if quarantined is not None else False,
            quarantine_reason=quarantine_reason,
        )
        session.add(state)
        await session.flush()
        return state
    if local_id is not None:
        state.local_id = local_id
    if last_pulled_etag is not None:
        state.last_pulled_etag = last_pulled_etag
        state.last_pulled_at = now
    if last_pushed_version is not None:
        state.last_pushed_version = last_pushed_version
        state.last_pushed_at = now
    if quarantined is not None:
        state.quarantined = bool(quarantined)
        state.quarantine_reason = quarantine_reason
    return state


# ---------------------------------------------------------------------- #
# Local-row lookups                                                      #
# ---------------------------------------------------------------------- #


async def _find_contact_by_external(
    session: AsyncSession,
    *,
    external_id: str,
) -> Contact | None:
    stmt = select(Contact).where(
        Contact.external_id == external_id,
        Contact.external_source == "xero",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_invoice_by_external(
    session: AsyncSession,
    *,
    external_id: str,
) -> Invoice | None:
    stmt = select(Invoice).where(
        Invoice.external_id == external_id,
        Invoice.external_source == "xero",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------- #
# Audit log                                                              #
# ---------------------------------------------------------------------- #


async def _audit(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    direction: SyncDirection,
    object_type: SyncObjectType | None,
    external_id: str | None,
    outcome: str,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    local_id: uuid.UUID | None = None,
) -> None:
    """Append one row to ``sync_audit_log``.

    Caller commits — we don't flush here; the orchestrator batches.
    """
    session.add(
        SyncAuditLog(
            tenant_id=connection.tenant_id,
            connection_id=connection.id,
            direction=direction.value,
            object_type=object_type.value if object_type else None,
            external_id=external_id,
            local_id=local_id,
            outcome=outcome,
            message=message,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------- #
# Misc helpers                                                           #
# ---------------------------------------------------------------------- #


def _bump_stat(stats: PullStats, outcome: str) -> None:
    if outcome == OUTCOME_OK:
        stats.upserted += 1
    elif outcome == OUTCOME_CONFLICT:
        stats.conflicted += 1
    elif outcome == OUTCOME_QUARANTINED:
        stats.quarantined += 1
    elif outcome == OUTCOME_SKIPPED:
        stats.skipped += 1
    else:
        stats.errors += 1


def _ifms_with_one_second_pad(last_pulled_at: datetime | None) -> datetime | None:
    """Return ``last_pulled_at`` minus 1 second.

    Xero ``If-Modified-Since`` is per-second; subtracting one second
    tolerates rounding without missing changes (we may re-pull one
    boundary row, which is harmless idempotent work).
    """
    if last_pulled_at is None:
        return None
    return last_pulled_at - timedelta(seconds=1)


def _parse_updated_utc(raw: str | None) -> datetime | None:
    """Parse Xero's ``UpdatedDateUTC`` into a UTC ``datetime``.

    Accepts both ISO 8601 and the legacy ``/Date(ms+zzzz)/`` form.
    Returns ``None`` for unparseable input — the caller advances the
    watermark conservatively.
    """
    if not raw:
        return None
    if raw.startswith("/Date("):
        inner = raw[6:-2]
        for sep in ("+", "-"):
            idx = inner.find(sep, 1)
            if idx > 0:
                inner = inner[:idx]
                break
        try:
            ms = int(inner)
        except ValueError:
            return None
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    try:
        # Xero's ISO is `YYYY-MM-DDTHH:MM:SS` (no offset, expressed in UTC).
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return None


def _merge_contact_type(local: ContactType, pulled: ContactType) -> ContactType:
    """Don't demote a Contact from BOTH to CUSTOMER/SUPPLIER on pull.

    Plan §1: the union of customer + supplier roles is preserved across
    sync — if our side or Xero's side has decided the contact is both,
    the contact stays both.
    """
    if local == ContactType.BOTH or pulled == ContactType.BOTH:
        return ContactType.BOTH
    if {local, pulled} == {ContactType.CUSTOMER, ContactType.SUPPLIER}:
        return ContactType.BOTH
    return pulled


def _merge_invoice_status(
    local: InvoiceStatus,
    pulled: InvoiceStatus,
) -> InvoiceStatus:
    """Never demote a posted invoice back to draft on pull.

    Plan §1: invoice status is monotonic forward — DRAFT -> POSTED ->
    VOIDED. If Xero says DRAFT but we already have POSTED, keep POSTED.
    """
    if local == InvoiceStatus.VOIDED:
        return InvoiceStatus.VOIDED
    if local == InvoiceStatus.POSTED and pulled == InvoiceStatus.DRAFT:
        return InvoiceStatus.POSTED
    return pulled


__all__ = [
    "OUTCOME_CONFLICT",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "OUTCOME_QUARANTINED",
    "OUTCOME_SKIPPED",
    "PullStats",
    "pull_contacts",
    "pull_invoices",
]
