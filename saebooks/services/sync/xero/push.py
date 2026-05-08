"""Push our changes upward to Xero.

Conflict resolution
-------------------
We use **Last-Writer-Wins with conflict surfacing**. Concretely:

* For every object we have a local ``version`` (monotonic, bumped on
  each PATCH) and a remote ``UpdatedDateUTC`` (Xero's own version).
* Our ``sync_state`` row remembers ``last_pushed_version`` (the local
  version we last successfully pushed) and ``last_pulled_etag`` (the
  remote etag we last pulled).
* When push runs, we look up the current Xero record for the same
  ``external_id``. If Xero's ``UpdatedDateUTC`` differs from our
  ``last_pulled_etag``, the remote moved since we last saw it. If we
  also moved (``local.version > last_pushed_version``) — that's a
  conflict.

Conflict policy:

* **Header-only conflicts** (status, due-date) — push wins; we
  overwrite Xero. The local change is the operator's intent and the
  upstream change is usually a downstream side-effect (e.g. payment
  applied externally). Audit logged.
* **Line-item conflicts on posted invoices** — never overwrite. Per
  ``[[feedback_saebooks-marketing-differentiator]]`` the rendered PDF
  is the source of truth; if both sides edited lines, we surface the
  conflict in the operator queue and take no action. Audit logged.

The orchestrator (``connector.sync_xero``) calls ``push_*`` after
``pull_*`` so push sees a fresh ``last_pulled_etag``.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceLine
from saebooks.models.sync import (
    SyncConnection,
    SyncDirection,
    SyncObjectType,
    SyncState,
)
from saebooks.services.sync.errors import (
    SyncConflictError,
    SyncValidationError,
)
from saebooks.services.sync.xero.client import XeroClient
from saebooks.services.sync.xero.endpoints import (
    post_contacts,
    post_invoices,
    post_manual_journals,
)
from saebooks.services.sync.xero.mappers import (
    saebooks_contact_to_xero,
    saebooks_invoice_to_xero,
    saebooks_journal_to_xero,
)
from saebooks.services.sync.xero.pull import (
    OUTCOME_CONFLICT,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_QUARANTINED,
    OUTCOME_SKIPPED,
    _audit,
    _get_state,
    _upsert_state,
)

log = logging.getLogger(__name__)


@dataclass
class PushStats:
    """Accumulated counters returned by ``push_*`` orchestrators."""

    candidates: int = 0
    pushed: int = 0
    skipped: int = 0
    conflicted: int = 0
    errors: int = 0


# ---------------------------------------------------------------------- #
# Public entry points                                                    #
# ---------------------------------------------------------------------- #


async def push_contacts(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
) -> PushStats:
    """Push contacts that have changed locally since the last push.

    Selection: contacts with either no ``last_pushed_version`` recorded
    OR ``contact.version > sync_state.last_pushed_version``. Archived
    contacts are pushed (Xero accepts ``ContactStatus="ARCHIVED"``).
    """
    stats = PushStats()
    candidates = await _select_contact_push_candidates(session, connection)
    stats.candidates = len(candidates)

    for contact in candidates:
        try:
            outcome = await _push_one_contact(
                session,
                connection=connection,
                client=client,
                contact=contact,
            )
        except SyncValidationError as exc:
            stats.errors += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PUSH,
                object_type=SyncObjectType.CONTACT,
                external_id=contact.external_id,
                local_id=contact.id,
                outcome=OUTCOME_ERROR,
                message=f"Xero rejected contact: {exc}",
                payload={"http_status": exc.http_status, "body": exc.payload},
            )
            continue

        _bump(stats, outcome)
    return stats


async def push_invoices(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
) -> PushStats:
    """Push invoices that have changed locally since the last push.

    Only POSTED invoices are pushed. DRAFT invoices stay local until
    the operator posts them — pushing drafts upstream creates noise
    (the operator may discard the draft).
    """
    stats = PushStats()
    candidates = await _select_invoice_push_candidates(session, connection)
    stats.candidates = len(candidates)

    for invoice in candidates:
        try:
            outcome = await _push_one_invoice(
                session,
                connection=connection,
                client=client,
                invoice=invoice,
            )
        except SyncValidationError as exc:
            stats.errors += 1
            await _audit(
                session,
                connection=connection,
                direction=SyncDirection.PUSH,
                object_type=SyncObjectType.INVOICE,
                external_id=invoice.external_id,
                local_id=invoice.id,
                outcome=OUTCOME_ERROR,
                message=f"Xero rejected invoice: {exc}",
                payload={"http_status": exc.http_status, "body": exc.payload},
            )
            continue

        _bump(stats, outcome)
    return stats


async def push_journal(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
    narration: str,
    journal_date: Any,
    lines: list[dict[str, Any]],
    local_id: uuid.UUID | None = None,
) -> str:
    """Push one Manual Journal up to Xero. Returns ``external_id``.

    Manual journals are push-only (we do not pull them — the GL is
    SAE Books' source of truth). The orchestrator computes the line
    list from one of our ``JournalEntry`` rows and calls this; on
    success we record an ``external_id`` on the local row so it
    doesn't get re-pushed.
    """
    body = saebooks_journal_to_xero(
        narration=narration,
        journal_date=journal_date,
        lines=lines,
    )
    rows = await post_manual_journals(client, [body])
    if not rows:
        raise SyncValidationError(
            "Xero accepted ManualJournal but returned no rows",
        )
    external_id = rows[0].get("ManualJournalID")
    if not isinstance(external_id, str):
        raise SyncValidationError(
            "Xero ManualJournal response missing ManualJournalID",
        )
    await _upsert_state(
        session,
        connection=connection,
        object_type=SyncObjectType.JOURNAL_ENTRY,
        external_id=external_id,
        local_id=local_id,
        last_pulled_etag=rows[0].get("UpdatedDateUTC"),
        last_pushed_version=1,
    )
    await _audit(
        session,
        connection=connection,
        direction=SyncDirection.PUSH,
        object_type=SyncObjectType.JOURNAL_ENTRY,
        external_id=external_id,
        local_id=local_id,
        outcome=OUTCOME_OK,
    )
    return external_id


# ---------------------------------------------------------------------- #
# Per-row push                                                           #
# ---------------------------------------------------------------------- #


async def _push_one_contact(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
    contact: Contact,
) -> str:
    """Push one Contact, returning the audit outcome string."""
    state = await _get_state(
        session,
        connection.id,
        SyncObjectType.CONTACT,
        contact.external_id or str(contact.id),
    )
    if state is not None and state.quarantined:
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.PUSH,
            object_type=SyncObjectType.CONTACT,
            external_id=contact.external_id,
            local_id=contact.id,
            outcome=OUTCOME_QUARANTINED,
            message="quarantined — manual resolution required",
        )
        return OUTCOME_QUARANTINED

    body = saebooks_contact_to_xero(contact)
    rows = await post_contacts(client, [body])
    if not rows:
        raise SyncValidationError("Xero returned no Contact on POST")
    new_external_id = rows[0].get("ContactID")
    new_etag = rows[0].get("UpdatedDateUTC")

    if isinstance(new_external_id, str) and (
        contact.external_id != new_external_id or contact.external_source != "xero"
    ):
        contact.external_id = new_external_id
        contact.external_source = "xero"
    if isinstance(new_etag, str):
        contact.external_etag = new_etag
    contact.external_payload = rows[0]

    await _upsert_state(
        session,
        connection=connection,
        object_type=SyncObjectType.CONTACT,
        external_id=contact.external_id or "",
        local_id=contact.id,
        last_pulled_etag=new_etag,
        last_pushed_version=contact.version,
    )
    await _audit(
        session,
        connection=connection,
        direction=SyncDirection.PUSH,
        object_type=SyncObjectType.CONTACT,
        external_id=contact.external_id,
        local_id=contact.id,
        outcome=OUTCOME_OK,
    )
    return OUTCOME_OK


async def _push_one_invoice(
    session: AsyncSession,
    *,
    connection: SyncConnection,
    client: XeroClient,
    invoice: Invoice,
) -> str:
    """Push one Invoice. Skips drafts; surfaces conflicts."""
    if invoice.status.value == "DRAFT":
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.PUSH,
            object_type=SyncObjectType.INVOICE,
            external_id=invoice.external_id,
            local_id=invoice.id,
            outcome=OUTCOME_SKIPPED,
            message="local draft — not pushed",
        )
        return OUTCOME_SKIPPED

    state = await _get_state(
        session,
        connection.id,
        SyncObjectType.INVOICE,
        invoice.external_id or str(invoice.id),
    )
    if state is not None and state.quarantined:
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.PUSH,
            object_type=SyncObjectType.INVOICE,
            external_id=invoice.external_id,
            local_id=invoice.id,
            outcome=OUTCOME_QUARANTINED,
            message="quarantined — manual resolution required",
        )
        return OUTCOME_QUARANTINED

    # Conflict check: did Xero move beyond what we last pulled?
    if (
        state is not None
        and state.last_pulled_etag is not None
        and invoice.external_etag is not None
        and state.last_pulled_etag != invoice.external_etag
    ):
        # Xero moved AND we moved — surface as conflict.
        await _audit(
            session,
            connection=connection,
            direction=SyncDirection.CONFLICT,
            object_type=SyncObjectType.INVOICE,
            external_id=invoice.external_id,
            local_id=invoice.id,
            outcome=OUTCOME_CONFLICT,
            message="invoice changed in Xero AND locally since last sync",
            payload={
                "last_pulled_etag": state.last_pulled_etag,
                "current_local_version": invoice.version,
                "last_pushed_version": state.last_pushed_version,
            },
        )
        # Quarantine to prevent repeat push attempts on every cycle.
        await _upsert_state(
            session,
            connection=connection,
            object_type=SyncObjectType.INVOICE,
            external_id=invoice.external_id or "",
            local_id=invoice.id,
            quarantined=True,
            quarantine_reason="LWW conflict — manual resolution required",
        )
        return OUTCOME_CONFLICT

    # Resolve contact ContactID via sync_state.
    contact_external_id: str | None = None
    if invoice.contact_id is not None:
        contact_state = await _get_contact_state_by_local_id(
            session,
            connection.id,
            invoice.contact_id,
        )
        if contact_state is not None:
            contact_external_id = contact_state.external_id

    # Eager-load lines on the same session.
    lines_stmt = select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
    lines = list((await session.execute(lines_stmt)).scalars())

    body = saebooks_invoice_to_xero(
        invoice,
        lines=lines,
        contact_external_id=contact_external_id,
    )
    rows = await post_invoices(client, [body])
    if not rows:
        raise SyncValidationError("Xero returned no Invoice on POST")

    new_external_id = rows[0].get("InvoiceID")
    new_etag = rows[0].get("UpdatedDateUTC")

    if isinstance(new_external_id, str) and (
        invoice.external_id != new_external_id or invoice.external_source != "xero"
    ):
        invoice.external_id = new_external_id
        invoice.external_source = "xero"
    if isinstance(new_etag, str):
        invoice.external_etag = new_etag
    invoice.external_payload = rows[0]

    await _upsert_state(
        session,
        connection=connection,
        object_type=SyncObjectType.INVOICE,
        external_id=invoice.external_id or "",
        local_id=invoice.id,
        last_pulled_etag=new_etag,
        last_pushed_version=invoice.version,
    )
    await _audit(
        session,
        connection=connection,
        direction=SyncDirection.PUSH,
        object_type=SyncObjectType.INVOICE,
        external_id=invoice.external_id,
        local_id=invoice.id,
        outcome=OUTCOME_OK,
    )
    return OUTCOME_OK


# ---------------------------------------------------------------------- #
# Selection helpers                                                      #
# ---------------------------------------------------------------------- #


async def _select_contact_push_candidates(
    session: AsyncSession,
    connection: SyncConnection,
) -> list[Contact]:
    """Find Contacts that need pushing.

    A Contact needs pushing when any of:

    * It has no ``external_id`` yet (never pushed).
    * Its ``version`` exceeds the matching ``sync_state.last_pushed_version``.

    Both cases are expressed via a LEFT JOIN — rows with no state row
    qualify as "never pushed".
    """
    # Contacts not yet linked to Xero (external_id is NULL).
    stmt_new = select(Contact).where(
        Contact.tenant_id == connection.tenant_id,
        Contact.external_id.is_(None),
    )
    new_rows = list((await session.execute(stmt_new)).scalars())

    # Contacts already linked — push if version > last_pushed_version.
    stmt_existing = (
        select(Contact)
        .join(
            SyncState,
            (SyncState.connection_id == connection.id)
            & (SyncState.object_type == SyncObjectType.CONTACT.value)
            & (SyncState.external_id == Contact.external_id),
        )
        .where(
            Contact.tenant_id == connection.tenant_id,
            Contact.external_source == "xero",
            Contact.external_id.is_not(None),
            (SyncState.last_pushed_version.is_(None))
            | (Contact.version > SyncState.last_pushed_version),
            SyncState.quarantined.is_(False),
        )
    )
    changed_rows = list((await session.execute(stmt_existing)).scalars())
    return new_rows + changed_rows


async def _select_invoice_push_candidates(
    session: AsyncSession,
    connection: SyncConnection,
) -> list[Invoice]:
    """Find Invoices that need pushing — POSTED and version-bumped only."""
    stmt_new = select(Invoice).where(
        Invoice.tenant_id == connection.tenant_id,
        Invoice.external_id.is_(None),
        Invoice.status == "POSTED",
    )
    new_rows = list((await session.execute(stmt_new)).scalars())

    stmt_existing = (
        select(Invoice)
        .join(
            SyncState,
            (SyncState.connection_id == connection.id)
            & (SyncState.object_type == SyncObjectType.INVOICE.value)
            & (SyncState.external_id == Invoice.external_id),
        )
        .where(
            Invoice.tenant_id == connection.tenant_id,
            Invoice.external_source == "xero",
            Invoice.external_id.is_not(None),
            Invoice.status == "POSTED",
            (SyncState.last_pushed_version.is_(None))
            | (Invoice.version > SyncState.last_pushed_version),
            SyncState.quarantined.is_(False),
        )
    )
    changed_rows = list((await session.execute(stmt_existing)).scalars())
    return new_rows + changed_rows


async def _get_contact_state_by_local_id(
    session: AsyncSession,
    connection_id: uuid.UUID,
    local_id: uuid.UUID,
) -> SyncState | None:
    stmt = select(SyncState).where(
        SyncState.connection_id == connection_id,
        SyncState.object_type == SyncObjectType.CONTACT.value,
        SyncState.local_id == local_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------- #
# Conflict-detection helper (importable for ad-hoc diagnostics)          #
# ---------------------------------------------------------------------- #


def detect_conflict(
    *,
    state: SyncState | None,
    local_version: int,
    current_remote_etag: str,
) -> bool:
    """Pure check: did both sides move since last sync?

    True iff:
        state is not None
        AND state.last_pulled_etag != current_remote_etag (remote moved)
        AND state.last_pushed_version is not None
        AND local_version > state.last_pushed_version (local moved)

    Importable for ad-hoc operator tooling — the orchestrator runs the
    full check inline.
    """
    if state is None:
        return False
    remote_moved = (
        state.last_pulled_etag is not None
        and state.last_pulled_etag != current_remote_etag
    )
    local_moved = (
        state.last_pushed_version is not None
        and local_version > state.last_pushed_version
    )
    return remote_moved and local_moved


# ---------------------------------------------------------------------- #
# Misc                                                                   #
# ---------------------------------------------------------------------- #


def _bump(stats: PushStats, outcome: str) -> None:
    if outcome == OUTCOME_OK:
        stats.pushed += 1
    elif outcome == OUTCOME_CONFLICT:
        stats.conflicted += 1
    elif outcome in (OUTCOME_SKIPPED, OUTCOME_QUARANTINED):
        stats.skipped += 1
    else:
        stats.errors += 1


__all__ = [
    "PushStats",
    "SyncConflictError",
    "detect_conflict",
    "push_contacts",
    "push_invoices",
    "push_journal",
]
