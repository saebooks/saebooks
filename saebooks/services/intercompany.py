"""Intercompany posting service — LOCAL (same-tenant) Phase 1.

Posts a **linked reciprocal pair** of journal entries for an economic event
between two companies co-resident in ONE tenant DB. Both legs post inside a
**single transaction** via the Phase-0 ``journal.post_in_txn(commit=False)``
primitive and a single trailing commit: if either leg fails to post, neither
posts (no half-pair, no orphan).

What a pair is
--------------
An ``IcTxn`` row records the shared economic event. Two ``JournalEntry`` rows —
one per company — record the local double-entry; each is stamped
``origin=INTERCOMPANY``, ``source_type='ic_txn'``, ``source_id=<ic_txn.id>``.
Two ``IcLeg`` rows link each JE to the ``IcTxn`` (``side`` ORIGINATOR /
COUNTERPARTY). The reciprocal "Due to/from" control accounts come from the two
``IcEdge`` rows (one per company), NOT from caller free-text, so the balance
hits the declared control account on each side.

Directors-loan-style example (the §3 personal -> SAE funding edge, modelled as
a same-tenant pair for the LOCAL primitive — the REAL personal<->primary edge is
cross-DB / REMOTE and awaits Phase 3, see ``TODO(remote-relay)`` below)::

    # Originator = personal company; counterparty = SAE company.
    # Edge (originator side): control_account = personal "Loan to SAE" (ASSET).
    # Edge (counterparty side): control_account = SAE "Directors Loan" (LIABILITY).
    await post_local_pair(
        session,
        tenant_id=tenant_id,
        originator_company_id=personal_id,
        counterparty_company_id=sae_id,
        amount=Decimal("5000.00"),
        entry_date=date(2026, 6, 6),
        description="Director funds SAE working capital",
        # Originator: Dr Loan-to-SAE (control), Cr Bank (contra).
        originator_contra_account_id=personal_bank_id,
        # Counterparty: Dr Bank (contra), Cr Directors-Loan (control).
        counterparty_contra_account_id=sae_bank_id,
    )

The sign convention is fixed by ``DIRECTION``: on the ORIGINATOR side the
control account is **debited** (a right / receivable / "due from") and the
contra credited; on the COUNTERPARTY side the control account is **credited** (an
obligation / "due to") and the contra debited. A caller wanting the opposite
economic direction swaps which company is the originator.

No GST is posted: the legs carry no ``gst_amount`` and the control accounts are
balance-sheet accounts (ASSET / LIABILITY), so ``auto_post_gst_lines`` is a
no-op (balance-sheet movement, no GST — design §3).

REMOTE relay
------------
Cross-DB partners (a company in a *different* tenant stack — e.g. Richard's real
personal-tenant <-> primary-tenant directors-loan edge) CANNOT use this primitive:
there is no shared transaction across two Postgres servers. That is the Phase-3
broker relay (outbox/inbox, Ed25519 signing, per-edge tokens). The seam is
marked with ``TODO(remote-relay)`` where a REMOTE edge would branch.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.models.account import Account
from saebooks.models.ic import (
    IcEdge,
    IcEdgeDirection,
    IcEdgeRelayStatus,
    IcEdgeTopology,
    IcInbox,
    IcInboxStatus,
    IcLeg,
    IcLegSide,
    IcOutbox,
    IcOutboxStatus,
    IcTxn,
    IcTxnStatus,
)
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine, JournalOrigin
from saebooks.services import journal as journal_svc
from saebooks.services.ic_relay import keys as relay_keys
from saebooks.services.ic_relay import protocol as relay_protocol
from saebooks.services.ic_relay import signing as relay_signing
from saebooks.services.journal import PostingError


class IntercompanyError(Exception):
    """Raised when an intercompany pair cannot be assembled or posted."""


class RemoteRelayDisabled(IntercompanyError):
    """Raised when a REMOTE edge is posted while the relay flag is OFF.

    The default-off ``SAEBOOKS_IC_REMOTE_RELAY_ENABLED`` flag (plan D4) is the
    primary reversibility lever: a REMOTE post is refused entirely (no local
    leg, no outbox row) until Richard signs off a per-stack go-live. LOCAL
    (same-tenant) posting is never affected by this flag.
    """


async def _resolve_edge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    partner_company_id: uuid.UUID,
    direction: IcEdgeDirection,
) -> IcEdge:
    """Fetch the IcEdge for (company -> partner) in the given direction.

    The edge is the capability: an economic event between two companies is only
    allowed along a pre-declared edge whose ``control_account_id`` bounds where
    the balance lands. A missing edge is a hard ``IntercompanyError`` (an
    opaque message — never leak the partner/account ids of a row the caller is
    not entitled to enumerate).
    """
    row = (
        await session.execute(
            select(IcEdge).where(
                IcEdge.tenant_id == tenant_id,
                IcEdge.company_id == company_id,
                IcEdge.partner_company_id == partner_company_id,
                IcEdge.direction == direction,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise IntercompanyError(
            f"No {direction} intercompany edge declared for this company pair"
        )
    return row


async def _assert_account_owned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Refuse a contra account that is not a postable account OF this company.

    Belt-and-braces over the DB composite FK / coherence trigger: a leg's
    contra account must belong to the leg's own company and tenant and must not
    be a header (group) account. Opaque error — no sister-company id leak.
    """
    acct = (
        await session.execute(
            select(Account.id, Account.is_header).where(
                Account.id == account_id,
                Account.company_id == company_id,
                Account.tenant_id == tenant_id,
            )
        )
    ).first()
    if acct is None:
        raise IntercompanyError("Contra account does not belong to this company")
    if acct.is_header:
        raise IntercompanyError(
            "Cannot post to a header (group) account — these are CoA scaffolding"
        )


async def _build_leg_draft(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    entry_date: date,
    description: str | None,
    control_account_id: uuid.UUID,
    contra_account_id: uuid.UUID,
    amount: Decimal,
    debit_control: bool,
) -> JournalEntry:
    """Construct (but do NOT commit) one balanced two-line draft JE.

    ``debit_control`` True  => Dr control / Cr contra (ORIGINATOR: due-from).
    ``debit_control`` False => Cr control / Dr contra (COUNTERPARTY: due-to).

    Mirrors ``journal.create_draft`` line-building but stages on the caller's
    session WITHOUT committing, so the pair can post atomically. ``company_id``
    on each line is left unset — the 0152 BEFORE-INSERT trigger fills it from
    the parent entry and the composite FK guards cross-company lines.
    """
    ref = await journal_svc.next_ref(session)
    entry = JournalEntry(
        company_id=company_id,
        tenant_id=tenant_id,
        ref=ref,
        entry_date=entry_date,
        description=description,
        status=EntryStatus.DRAFT,
    )
    session.add(entry)
    await session.flush()

    if debit_control:
        control_debit, control_credit = amount, Decimal("0")
        contra_debit, contra_credit = Decimal("0"), amount
    else:
        control_debit, control_credit = Decimal("0"), amount
        contra_debit, contra_credit = amount, Decimal("0")

    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=1,
            account_id=control_account_id,
            description=description,
            debit=control_debit,
            credit=control_credit,
        )
    )
    session.add(
        JournalLine(
            entry_id=entry.id,
            line_no=2,
            account_id=contra_account_id,
            description=description,
            debit=contra_debit,
            credit=contra_credit,
        )
    )
    await session.flush()
    return entry


async def post_local_pair(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    originator_company_id: uuid.UUID,
    counterparty_company_id: uuid.UUID,
    amount: Decimal,
    entry_date: date,
    description: str | None = None,
    originator_contra_account_id: uuid.UUID,
    counterparty_contra_account_id: uuid.UUID,
    posted_by: str | None = None,
) -> IcTxn:
    """Post a linked reciprocal pair of JEs for a same-tenant (LOCAL) IC event.

    Atomic: both legs post inside ONE transaction via
    ``journal.post_in_txn(commit=False)`` and a single commit. If either leg
    fails (validation, balance, period lock, DB constraint) the whole pair
    rolls back — no half-pair, no orphan ``ic_legs`` / ``ic_txn``.

    Returns the persisted ``IcTxn`` (status ACTIVE). Both JEs are stamped
    ``origin=INTERCOMPANY``, ``source_type='ic_txn'``, ``source_id=ic_txn.id``;
    two ``IcLeg`` rows link them.

    Both companies MUST be in ``tenant_id`` (LOCAL). A reciprocal pair of
    ``IcEdge`` rows must already exist: an ORIGINATOR edge on
    ``originator_company_id`` -> ``counterparty_company_id`` and a COUNTERPARTY
    edge on ``counterparty_company_id`` -> ``originator_company_id``. The
    control accounts come from those edges (the balance-sheet allowlist), not
    from the caller.
    """
    if amount <= Decimal("0"):
        raise IntercompanyError("Intercompany amount must be positive")
    if originator_company_id == counterparty_company_id:
        raise IntercompanyError(
            "Originator and counterparty must be different companies"
        )

    # Resolve the originator edge FIRST — its topology decides LOCAL vs REMOTE.
    orig_edge = await _resolve_edge(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        partner_company_id=counterparty_company_id,
        direction=IcEdgeDirection.ORIGINATOR,
    )

    # REMOTE seam (Phase 3c): a REMOTE edge's partner lives in a DIFFERENT
    # tenant DB — there is NO shared transaction across two Postgres servers and
    # NO local counterparty edge to resolve. We branch into the broker outbox
    # path: post ONLY the originator leg + an ic_outbox row in THIS one local
    # txn; a dispatcher then relays a signed payload to the partner stack's
    # /ic/accept. ``post_local_pair`` is reserved for same-tenant pairs.
    if orig_edge.topology == IcEdgeTopology.REMOTE:
        raise IntercompanyError(
            "Originator edge is REMOTE — use post_remote_originator (the LOCAL "
            "post_local_pair primitive cannot span two tenant DBs)"
        )

    # LOCAL: resolve the reciprocal counterparty edge too. Both must exist.
    cpty_edge = await _resolve_edge(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        partner_company_id=originator_company_id,
        direction=IcEdgeDirection.COUNTERPARTY,
    )

    # Validate caller-supplied contra accounts belong to the right company.
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        account_id=originator_contra_account_id,
    )
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        account_id=counterparty_contra_account_id,
    )

    # The shared economic event.
    ic_txn = IcTxn(
        tenant_id=tenant_id,
        company_id=originator_company_id,
        description=description,
        status=IcTxnStatus.ACTIVE,
    )
    session.add(ic_txn)
    await session.flush()

    # Originator leg: Dr control (due-from) / Cr contra.
    orig_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=orig_edge.control_account_id,
        contra_account_id=originator_contra_account_id,
        amount=amount,
        debit_control=True,
    )
    # Counterparty leg: Cr control (due-to) / Dr contra.
    cpty_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=counterparty_company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=cpty_edge.control_account_id,
        contra_account_id=counterparty_contra_account_id,
        amount=amount,
        debit_control=False,
    )

    # Post BOTH legs without committing — atomicity is purely local.
    await journal_svc.post_in_txn(
        session,
        orig_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=ic_txn.id,
    )
    await journal_svc.post_in_txn(
        session,
        cpty_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=ic_txn.id,
    )

    # Link each JE to the shared txn.
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=originator_company_id,
            ic_txn_id=ic_txn.id,
            journal_entry_id=orig_entry.id,
            side=IcLegSide.ORIGINATOR,
        )
    )
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=counterparty_company_id,
            ic_txn_id=ic_txn.id,
            journal_entry_id=cpty_entry.id,
            side=IcLegSide.COUNTERPARTY,
        )
    )

    # Single commit — all six objects (ic_txn, 2 JEs+lines, 2 ic_legs) land
    # together or not at all.
    await session.commit()
    return ic_txn


async def post_remote_originator(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    originator_company_id: uuid.UUID,
    edge_id: uuid.UUID,
    amount: Decimal,
    entry_date: date,
    description: str | None = None,
    posted_by: str | None = None,
    settings: Settings | None = None,
) -> tuple[IcTxn, IcOutbox]:
    """Post the ORIGINATOR leg of a REMOTE intercompany event + an outbox row.

    The cross-DB analogue of ``post_local_pair`` for the originator side. In ONE
    local transaction it: mints the shared ``ic_txn_id``, posts the originator
    local leg (Dr the edge control account / Cr the contra), links one
    ``IcLeg`` (ORIGINATOR), and writes one ``ic_outbox`` row carrying the signed
    canonical payload. The local books are CORRECT AND FINAL on commit,
    regardless of whether the partner is reachable — a dispatcher relays the
    outbox row asynchronously.

    Gated by ``SAEBOOKS_IC_REMOTE_RELAY_ENABLED`` (default OFF): with the flag
    off this raises :class:`RemoteRelayDisabled` BEFORE any write, so a REMOTE
    edge is fully inert until a named go-live (plan D4). The edge must also be
    ``topology=REMOTE`` and ``relay_status=ACTIVE`` (only the authoriser flow,
    holding grants on both tenants, can set ACTIVE — §4.1).

    No account ids cross the wire (§4.3): the payload carries only the amount,
    date, ids, nonce and freshness anchor. GST is structurally absent — the
    control account is balance-sheet, so ``auto_post_gst_lines`` is a no-op.
    """
    cfg = settings if settings is not None else _default_settings
    if not cfg.ic_remote_relay_enabled:
        raise RemoteRelayDisabled(
            "remote relay disabled — SAEBOOKS_IC_REMOTE_RELAY_ENABLED is off; "
            "no REMOTE intercompany leg or outbox row was written"
        )
    if amount <= Decimal("0"):
        raise IntercompanyError("Intercompany amount must be positive")

    edge = (
        await session.execute(
            select(IcEdge).where(
                IcEdge.id == edge_id,
                IcEdge.tenant_id == tenant_id,
                IcEdge.company_id == originator_company_id,
                IcEdge.direction == IcEdgeDirection.ORIGINATOR,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise IntercompanyError("No ORIGINATOR intercompany edge for this company")
    if edge.topology != IcEdgeTopology.REMOTE:
        raise IntercompanyError("Edge is not a REMOTE edge")
    if edge.relay_status != IcEdgeRelayStatus.ACTIVE:
        raise IntercompanyError(
            "REMOTE edge is not ACTIVE — it has not been authorised for relay"
        )
    if edge.partner_tenant_id is None or edge.relay_privkey_ciphertext is None:
        raise IntercompanyError(
            "REMOTE edge is missing partner tenant / signing key (not enabled)"
        )
    if edge.relay_contra_account_id is None:
        raise IntercompanyError(
            "REMOTE edge has no contra (bank/clearing) account declared"
        )

    # Both accounts come from the edge row — NO account id crosses the wire.
    originator_contra_account_id = edge.relay_contra_account_id
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        account_id=originator_contra_account_id,
    )

    # Shared event id, chosen by the originator and carried in the payload.
    ic_txn = IcTxn(
        tenant_id=tenant_id,
        company_id=originator_company_id,
        description=description,
        status=IcTxnStatus.ACTIVE,
    )
    session.add(ic_txn)
    await session.flush()

    # Originator leg: Dr control (due-from) / Cr contra. Same sign convention
    # as the LOCAL path's originator side.
    orig_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=originator_company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=edge.control_account_id,
        contra_account_id=originator_contra_account_id,
        amount=amount,
        debit_control=True,
    )
    await journal_svc.post_in_txn(
        session,
        orig_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=ic_txn.id,
    )
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=originator_company_id,
            ic_txn_id=ic_txn.id,
            journal_entry_id=orig_entry.id,
            side=IcLegSide.ORIGINATOR,
        )
    )

    # Build + sign the canonical payload, then stage the outbox row in the SAME
    # txn so the local leg and the relay intent land atomically.
    nonce = uuid.uuid4()
    payload = relay_protocol.build_payload(
        ic_txn_id=ic_txn.id,
        edge_id=edge.id,
        src_tenant_id=tenant_id,
        dst_tenant_id=edge.partner_tenant_id,
        amount=amount,
        entry_date=entry_date,
        description=description,
        nonce=nonce,
    )
    private_raw = relay_keys.unwrap_private_key(
        edge.relay_privkey_ciphertext.decode("ascii")
        if isinstance(edge.relay_privkey_ciphertext, (bytes, bytearray))
        else edge.relay_privkey_ciphertext,
        settings=cfg,
    )
    signature = relay_signing.sign(
        relay_signing.canonical_payload(payload), private_raw
    )
    outbox = IcOutbox(
        tenant_id=tenant_id,
        company_id=originator_company_id,
        ic_txn_id=ic_txn.id,
        edge_id=edge.id,
        idempotency_key=ic_txn.id,
        nonce=nonce,
        payload_json=payload,
        signature=signature,
        status=IcOutboxStatus.PENDING,
    )
    session.add(outbox)

    # Single commit — originator leg + ic_leg + ic_outbox land together or not
    # at all. The reciprocal leg is the partner's job, relayed asynchronously.
    await session.commit()
    return ic_txn, outbox


async def accept_remote_counterparty(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    edge_id: uuid.UUID,
    ic_txn_id: uuid.UUID,
    nonce: uuid.UUID,
    amount: Decimal,
    entry_date: date,
    description: str | None,
    payload_json: dict[str, object],
    signature: bytes,
    posted_by: str | None = None,
) -> IcTxn:
    """Post the COUNTERPARTY reciprocal leg of a RECEIVED REMOTE event + inbox.

    Called by the ``/ic/accept`` webhook AFTER it has verified the per-edge
    token, the Ed25519 signature, freshness, and routing (dst_tenant == self).
    In ONE local txn it: mints a NEW local ``ic_txn`` (carrying the
    originator-chosen ``ic_txn_id`` only on the inbox row as the external link),
    posts the reciprocal leg (Cr the edge control account / Dr the contra),
    links one ``IcLeg`` (COUNTERPARTY), and writes the ``ic_inbox`` audit row
    (status POSTED, journal_entry_id set).

    Idempotency / replay are enforced by the DB: ``UNIQUE(tenant_id,
    ic_txn_id)`` and ``UNIQUE(tenant_id, nonce)`` on ``ic_inbox``. A duplicate
    delivery hits one of those and the caller catches the IntegrityError to
    return the prior ack WITHOUT posting again. This function assumes the
    caller has already done the dedupe pre-check; the unique constraints are the
    last-line race guard.

    The account NEVER comes from the wire — it is the receiver's OWN
    ``counterparty_contra_account_id`` plus the edge-declared control account
    (§4.3). GST is structurally absent (balance-sheet control account).
    """
    edge = (
        await session.execute(
            select(IcEdge).where(
                IcEdge.id == edge_id,
                IcEdge.tenant_id == tenant_id,
                IcEdge.direction == IcEdgeDirection.COUNTERPARTY,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise IntercompanyError("No COUNTERPARTY intercompany edge for this tenant")
    if edge.topology != IcEdgeTopology.REMOTE:
        raise IntercompanyError("Edge is not a REMOTE edge")
    if edge.relay_status != IcEdgeRelayStatus.ACTIVE:
        raise IntercompanyError("REMOTE edge is not ACTIVE")
    if amount <= Decimal("0"):
        raise IntercompanyError("Intercompany amount must be positive")
    if edge.relay_contra_account_id is None:
        raise IntercompanyError(
            "REMOTE edge has no contra (bank/clearing) account declared"
        )

    # Both accounts come from the receiver's OWN edge — the wire carries none.
    counterparty_contra_account_id = edge.relay_contra_account_id
    await _assert_account_owned(
        session,
        tenant_id=tenant_id,
        company_id=edge.company_id,
        account_id=counterparty_contra_account_id,
    )

    local_txn = IcTxn(
        tenant_id=tenant_id,
        company_id=edge.company_id,
        description=description,
        status=IcTxnStatus.ACTIVE,
    )
    session.add(local_txn)
    await session.flush()

    # Counterparty leg: Cr control (due-to) / Dr contra.
    cpty_entry = await _build_leg_draft(
        session,
        tenant_id=tenant_id,
        company_id=edge.company_id,
        entry_date=entry_date,
        description=description,
        control_account_id=edge.control_account_id,
        contra_account_id=counterparty_contra_account_id,
        amount=amount,
        debit_control=False,
    )
    await journal_svc.post_in_txn(
        session,
        cpty_entry.id,
        posted_by=posted_by,
        tenant_id=tenant_id,
        origin=JournalOrigin.INTERCOMPANY,
        source_type="ic_txn",
        source_id=local_txn.id,
    )
    session.add(
        IcLeg(
            tenant_id=tenant_id,
            company_id=edge.company_id,
            ic_txn_id=local_txn.id,
            journal_entry_id=cpty_entry.id,
            side=IcLegSide.COUNTERPARTY,
        )
    )

    # Inbox audit row: ic_txn_id is the ORIGINATOR-chosen external id (the
    # idempotency key); the UNIQUE constraints are the last-line replay guard.
    inbox = IcInbox(
        tenant_id=tenant_id,
        company_id=edge.company_id,
        ic_txn_id=ic_txn_id,
        edge_id=edge.id,
        nonce=nonce,
        payload_json=payload_json,
        signature=signature,
        journal_entry_id=cpty_entry.id,
        status=IcInboxStatus.POSTED,
    )
    session.add(inbox)

    # Single commit — reciprocal leg + ic_leg + ic_inbox land together.
    await session.commit()
    return local_txn


async def reverse_local_pair(
    session: AsyncSession,
    ic_txn_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    reversal_date: date | None = None,
    posted_by: str | None = None,
) -> IcTxn:
    """Settle/reverse an intercompany txn by reversing BOTH legs, linked.

    Reverses each leg's JE via ``journal.reverse`` (which posts a swapped
    mirror, flips the original to REVERSED, and audit-snapshots first), records
    each reversal JE as a new ``IcLeg`` against the SAME ``ic_txn`` (so the
    unwind stays linked and traceable both directions), and flips the
    ``IcTxn`` to ``REVERSED``.

    Note ``journal.reverse`` commits internally per leg; the two reversals are
    therefore not a single DB transaction, but each reversal is individually
    atomic and idempotency is protected by the ``IcTxn`` status guard (a
    REVERSED txn cannot be reversed again).
    """
    ic_txn = (
        await session.execute(
            select(IcTxn).where(
                IcTxn.id == ic_txn_id,
                IcTxn.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if ic_txn is None:
        raise IntercompanyError("Intercompany transaction not found")
    if ic_txn.status == IcTxnStatus.REVERSED:
        raise IntercompanyError("Intercompany transaction is already reversed")

    legs = (
        await session.execute(
            select(IcLeg).where(
                IcLeg.ic_txn_id == ic_txn_id,
                IcLeg.tenant_id == tenant_id,
            )
        )
    ).scalars().all()
    if not legs:
        raise IntercompanyError("Intercompany transaction has no legs to reverse")

    for leg in legs:
        try:
            reversal = await journal_svc.reverse(
                session,
                leg.journal_entry_id,
                reversal_date=reversal_date,
                posted_by=posted_by,
                tenant_id=tenant_id,
            )
        except PostingError as exc:  # surface as an IC-level failure
            raise IntercompanyError(
                f"Could not reverse intercompany leg: {exc}"
            ) from exc
        # Link the reversal JE to the same ic_txn (mirror side).
        mirror_side = (
            IcLegSide.COUNTERPARTY
            if leg.side == IcLegSide.ORIGINATOR
            else IcLegSide.ORIGINATOR
        )
        session.add(
            IcLeg(
                tenant_id=tenant_id,
                company_id=leg.company_id,
                ic_txn_id=ic_txn_id,
                journal_entry_id=reversal.id,
                # Distinct (company, txn, side) from the original leg: the
                # original leg already holds (company, txn, leg.side), so the
                # reversal records the mirror side for that company.
                side=mirror_side,
            )
        )

    ic_txn.status = IcTxnStatus.REVERSED
    await session.commit()
    return ic_txn
