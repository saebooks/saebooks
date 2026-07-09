"""Document Inbox service — the one ingest funnel + review state machine.

Issue #33 phase 1 (the Dext loop, EXPENSE only). Every capture surface
(multipart upload today; email-in in phase 3; agents via the same API)
calls :func:`ingest`, which owns the full funnel:

    sha256 → dedupe pre-check → vault ``POST /files`` (blob durable
    before any extraction attempt) → row insert with the partial-unique
    index as the race backstop → synchronous extraction.

Design rules (spec §5/§6):

* **Capture never blocks on the brain.** A model *soft-failure*
  (``extraction_error`` set in the result dict — the LiteLLM call
  answered but the output is unusable) lands the document in
  ``NEEDS_REVIEW`` with ``PARTIAL`` confidence. A *transport failure*
  (exception out of the extraction call or the vault download) puts the
  document back to ``RECEIVED`` with ``last_error`` set — the upload
  still succeeds, the phase-3 sweep (or a manual retry) picks it up.
* **The engine stores no blob bytes** — ``services/vault.py`` is the
  only byte path; this module holds vault file UUIDs only.
* **The state machine is server-enforced** — an illegal transition
  raises :class:`IllegalTransitionError`, which the router maps to 409.
* ``extract`` is the verbatim model output. Re-extraction may replace
  it wholesale (extraction is idempotent); reviewer edits never touch
  it — they live in ``extraction_override`` (enforced at the router).

The router (``api/v1/document_inbox.py``) is a thin HTTP shell; all
inbox logic lives here.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.inbox_document import (
    ExtractionConfidence,
    InboxDocument,
    InboxDocumentSource,
    InboxDocumentStatus,
    PublishedRecordKind,
)
from saebooks.models.inbox_email import InboxEmailAddress
from saebooks.models.supplier_rule import SupplierRule, SupplierRuleOrigin
from saebooks.services import ai_extraction
from saebooks.services import vault as vault_client

logger = logging.getLogger("saebooks.services.document_inbox")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DocumentInboxError(Exception):
    """Base class for document-inbox service errors."""


class SupplierRuleError(DocumentInboxError):
    """Invalid supplier-rule input (bad FK ownership, empty vendor key…).

    The router maps this to HTTP 422.
    """


class IllegalTransitionError(DocumentInboxError):
    """Raised when a status transition violates the spec §6 machine.

    The router maps this to HTTP 409.
    """

    def __init__(
        self, current: InboxDocumentStatus | str, target: InboxDocumentStatus | str
    ) -> None:
        super().__init__(
            f"illegal inbox document transition {current} -> {target}"
        )
        self.current = current
        self.target = target


# ---------------------------------------------------------------------------
# State machine (spec §6, server-enforced)
# ---------------------------------------------------------------------------

_S = InboxDocumentStatus

# RECEIVED → EXTRACTING → {NEEDS_REVIEW | READY | RECEIVED(retry) | FAILED};
# NEEDS_REVIEW ↔ READY (completeness recompute);
# {NEEDS_REVIEW, READY, FAILED} → PUBLISHED | REJECTED;
# DUPLICATE, PUBLISHED, REJECTED terminal.
#
# Deliberate extension beyond the spec's arrow diagram: EXTRACTING is
# reachable from NEEDS_REVIEW / READY / FAILED as well as RECEIVED —
# that is the "manual Retry re-runs it" path (spec §5: extraction is
# idempotent, so a re-run from any reviewable state is safe). Without
# FAILED → EXTRACTING the retry endpoint could never rescue a FAILED
# document, which is its whole purpose.
_LEGAL_TRANSITIONS: dict[InboxDocumentStatus, frozenset[InboxDocumentStatus]] = {
    _S.RECEIVED: frozenset({_S.EXTRACTING}),
    _S.EXTRACTING: frozenset(
        {_S.NEEDS_REVIEW, _S.READY, _S.RECEIVED, _S.FAILED}
    ),
    _S.NEEDS_REVIEW: frozenset(
        {_S.READY, _S.PUBLISHED, _S.REJECTED, _S.EXTRACTING}
    ),
    _S.READY: frozenset(
        {_S.NEEDS_REVIEW, _S.PUBLISHED, _S.REJECTED, _S.EXTRACTING}
    ),
    _S.FAILED: frozenset({_S.PUBLISHED, _S.REJECTED, _S.EXTRACTING}),
    _S.PUBLISHED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.DUPLICATE: frozenset(),
}

#: Terminal states — no transition out, and (spec §6) no mutation either:
#: PUBLISHED rows are immutable provenance; REJECTED / DUPLICATE are
#: closed audit rows.
TERMINAL_STATUSES: frozenset[InboxDocumentStatus] = frozenset(
    {_S.PUBLISHED, _S.REJECTED, _S.DUPLICATE}
)


def ensure_can_transition(
    current: InboxDocumentStatus | str, target: InboxDocumentStatus | str
) -> None:
    """Raise :class:`IllegalTransitionError` unless ``current -> target`` is legal."""
    cur = InboxDocumentStatus(current)
    tgt = InboxDocumentStatus(target)
    if tgt not in _LEGAL_TRANSITIONS[cur]:
        raise IllegalTransitionError(cur, tgt)


def transition(doc: InboxDocument, target: InboxDocumentStatus | str) -> None:
    """Apply a legal status transition to ``doc`` (no commit, no version bump)."""
    ensure_can_transition(doc.status, target)
    doc.status = InboxDocumentStatus(target)


# ---------------------------------------------------------------------------
# Completeness (NEEDS_REVIEW ↔ READY)
# ---------------------------------------------------------------------------


def merged_extract(doc: InboxDocument) -> dict[str, Any]:
    """The reviewer-effective view: ``extract`` shallow-overlaid by
    ``extraction_override``. Override keys win; ``line_items`` in the
    override replaces the extracted list wholesale.
    """
    merged: dict[str, Any] = dict(doc.extract or {})
    merged.update(doc.extraction_override or {})
    return merged


def is_ready(doc: InboxDocument) -> bool:
    """READY = one-click-publishable (spec §6): contact + account +
    tax code + total all present on the merged view. For EXPENSE the
    account/tax coding is per line, matching the extract shape — every
    line must carry ``account_id`` and ``tax_code_id``.

    Phase 2: supplier-rule suggestions count toward completeness —
    ``suggested_contact_id`` stands in for a missing merged
    ``contact_id``, and a document-level ``suggested_account_id`` /
    ``suggested_tax_code_id`` (a rule codes the whole document) stands
    in for the per-line ids. READY is still never auto-published: the
    IDs come from a human-authored/confirmed rule or from
    ``extraction_override``, and publish remains a deliberate act.
    """
    merged = merged_extract(doc)
    if not (merged.get("contact_id") or doc.suggested_contact_id):
        return False
    if merged.get("total") in (None, ""):
        return False
    lines = merged.get("line_items")
    if not isinstance(lines, list) or not lines:
        return False
    return all(
        isinstance(line, dict)
        and (line.get("account_id") or doc.suggested_account_id)
        and (line.get("tax_code_id") or doc.suggested_tax_code_id)
        for line in lines
    )


def recompute_completeness(doc: InboxDocument) -> None:
    """Flip NEEDS_REVIEW ↔ READY to match :func:`is_ready`.

    No-op in any other status — completeness only governs the two
    review states, never e.g. resurrects a FAILED document.
    """
    current = InboxDocumentStatus(doc.status)
    if current not in (_S.NEEDS_REVIEW, _S.READY):
        return
    target = _S.READY if is_ready(doc) else _S.NEEDS_REVIEW
    if target != current:
        transition(doc, target)


# ---------------------------------------------------------------------------
# Advisory near-duplicate (spec §6/§9, phase 4)
#
# sha256 dedupe catches identical bytes; a RE-SCAN of the same paper
# document produces different bytes. This advisory check flags
# (tenant, contact/vendor, invoice_number) collisions among the
# non-terminal inbox rows. It is ADVISORY only — a banner and a stat,
# never a blocked upload or an automatic DUPLICATE transition: the
# human reviewer is the authority (the standing anti-auto-publish
# posture applies to auto-rejection too).
# ---------------------------------------------------------------------------

_ADVISORY_TERMINAL = (
    InboxDocumentStatus.PUBLISHED.value,
    InboxDocumentStatus.REJECTED.value,
    InboxDocumentStatus.DUPLICATE.value,
)


def advisory_identity(doc: InboxDocument) -> tuple[str | None, str | None, str | None]:
    """``(invoice_number, contact_id, vendor_key)`` — the near-duplicate
    identity of a document, read from the reviewer-effective merged view
    (override wins over extract) plus the rule suggestion.

    ``invoice_number`` is case-folded/trimmed; ``vendor_key`` uses the
    supplier-rule normalisation so "BP  Wacol " and "bp wacol" collide.
    Any component may be ``None`` when unknown.
    """
    merged = merged_extract(doc)
    invoice_number = str(merged.get("invoice_number") or "").strip().lower() or None
    contact_id = str(merged.get("contact_id") or "").strip().lower() or None
    if contact_id is None and doc.suggested_contact_id is not None:
        contact_id = str(doc.suggested_contact_id).lower()
    vendor_key = normalise_vendor_key(merged.get("vendor_name"))
    return invoice_number, contact_id, vendor_key


def _advisory_matches(
    a: tuple[str | None, str | None, str | None],
    b: tuple[str | None, str | None, str | None],
) -> bool:
    """Two identities collide when the invoice numbers are equal AND the
    vendor matches. Contact is the stronger vendor signal: when both
    documents carry a contact, only the contacts decide (two suppliers
    that happen to share a display name must not collide); otherwise
    the normalised vendor names decide. A document with no vendor
    identity at all never matches — a bare invoice number like "1" is
    far too generic to warn on.
    """
    inv_a, contact_a, vendor_a = a
    inv_b, contact_b, vendor_b = b
    if not inv_a or inv_a != inv_b:
        return False
    if contact_a and contact_b:
        return contact_a == contact_b
    return bool(vendor_a and vendor_b and vendor_a == vendor_b)


async def find_advisory_duplicates(
    session: AsyncSession, doc: InboxDocument
) -> list[InboxDocument]:
    """Non-terminal sibling documents that look like the same underlying
    invoice as ``doc`` — same tenant, matching (contact/vendor,
    invoice_number) identity, oldest first. Empty when ``doc`` has no
    invoice number or no vendor identity.
    """
    identity = advisory_identity(doc)
    invoice_number, contact_id, vendor_key = identity
    if invoice_number is None or (contact_id is None and vendor_key is None):
        return []
    siblings = (
        (
            await session.execute(
                select(InboxDocument)
                .where(
                    InboxDocument.tenant_id == doc.tenant_id,
                    InboxDocument.id != doc.id,
                    InboxDocument.status.notin_(_ADVISORY_TERMINAL),
                )
                .order_by(InboxDocument.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [s for s in siblings if _advisory_matches(identity, advisory_identity(s))]


async def count_advisory_duplicates(
    session: AsyncSession, tenant_id: uuid.UUID
) -> int:
    """Stats counter: how many non-terminal documents share their
    (contact/vendor, invoice_number) identity with at least one other
    non-terminal document. Grouped by invoice number first, so the
    pairwise vendor comparison only runs inside each collision bucket.
    """
    docs = (
        (
            await session.execute(
                select(InboxDocument).where(
                    InboxDocument.tenant_id == tenant_id,
                    InboxDocument.status.notin_(_ADVISORY_TERMINAL),
                )
            )
        )
        .scalars()
        .all()
    )
    buckets: dict[str, list[tuple[str | None, str | None, str | None]]] = {}
    for d in docs:
        identity = advisory_identity(d)
        if identity[0] and (identity[1] or identity[2]):
            buckets.setdefault(identity[0], []).append(identity)
    flagged = 0
    for group in buckets.values():
        for i, identity in enumerate(group):
            if any(
                _advisory_matches(identity, other)
                for j, other in enumerate(group)
                if j != i
            ):
                flagged += 1
    return flagged


# ---------------------------------------------------------------------------
# Supplier rules (spec §6, phase 2) — deterministic, suggestion-only
# ---------------------------------------------------------------------------


def normalise_vendor_key(name: Any) -> str | None:
    """Normalised vendor name — lowercase, trimmed, whitespace collapsed.

    Returns ``None`` for empty/absent input so callers can skip matching
    outright rather than match on the empty string.
    """
    if name is None:
        return None
    key = " ".join(str(name).split()).lower()
    return key[:255] or None


def normalise_abn(abn: Any) -> str | None:
    """Digits-only 11-character Australian Business Number, or ``None``.

    Anything that does not reduce to exactly 11 digits is treated as
    absent — a mangled ABN must never produce a confident match.
    """
    if abn is None:
        return None
    digits = re.sub(r"\D", "", str(abn))
    return digits if len(digits) == 11 else None


def _rule_scope_clause(company_id: uuid.UUID | None):
    """Company scoping for matching: a routed document accepts rules for
    its company or tenant-wide rules; an unrouted document (NULL
    company) accepts any active rule — suggestions are prefill, and the
    reviewer still routes + confirms before publish."""
    if company_id is None:
        return None
    return or_(
        SupplierRule.company_id.is_(None),
        SupplierRule.company_id == company_id,
    )


async def match_supplier_rule(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    vendor_abn: Any = None,
    vendor_name: Any = None,
    company_id: uuid.UUID | None = None,
) -> SupplierRule | None:
    """Spec §6 matching: **ABN-exact → vendor_key-exact, first match
    wins** (the bank-rules posture — suggestion-only, no machine
    learning). Within a stage, a company-specific rule beats a
    tenant-wide one; ties break oldest-first (deterministic).
    """
    abn = normalise_abn(vendor_abn)
    key = normalise_vendor_key(vendor_name)
    scope = _rule_scope_clause(company_id)

    for stage_clause in (
        (SupplierRule.vendor_abn == abn) if abn else None,
        (SupplierRule.vendor_key == key) if key else None,
    ):
        if stage_clause is None:
            continue
        stmt = (
            select(SupplierRule)
            .where(
                SupplierRule.tenant_id == tenant_id,
                SupplierRule.active.is_(True),
                stage_clause,
            )
            .order_by(
                SupplierRule.company_id.is_(None),  # company-specific first
                SupplierRule.created_at,
            )
            .limit(1)
        )
        if scope is not None:
            stmt = stmt.where(scope)
        rule = (await session.execute(stmt)).scalars().first()
        if rule is not None:
            return rule
    return None


async def _apply_rule_matching(session: AsyncSession, doc: InboxDocument) -> None:
    """Fill (or refresh) the rule suggestions on ``doc`` from its merged
    extract. Called inside :func:`run_extraction` after ``extract`` is
    written — idempotent, like extraction itself: a match replaces the
    suggestions wholesale; no-match clears them only when they were
    rule-sourced (``supplier_rule_id`` set), leaving reviewer-set
    suggestions alone. No commit — the caller owns the transaction.
    """
    merged = merged_extract(doc)
    rule = await match_supplier_rule(
        session,
        doc.tenant_id,
        vendor_abn=merged.get("vendor_abn"),
        vendor_name=merged.get("vendor_name"),
        company_id=doc.company_id,
    )
    if rule is not None:
        doc.supplier_rule_id = rule.id
        doc.suggested_contact_id = rule.contact_id
        doc.suggested_account_id = rule.account_id
        doc.suggested_tax_code_id = rule.tax_code_id
    elif doc.supplier_rule_id is not None:
        doc.supplier_rule_id = None
        doc.suggested_contact_id = None
        doc.suggested_account_id = None
        doc.suggested_tax_code_id = None


def _uniform(values: list[Any]) -> Any:
    """The single value shared by every element, else ``None`` — a rule
    codes the whole document, so mixed-coding publishes teach nothing."""
    if not values:
        return None
    first = values[0]
    return first if all(v == first for v in values) else None


async def record_publish_rule_outcome(
    session: AsyncSession,
    doc: InboxDocument,
    *,
    contact_id: uuid.UUID,
    account_id: uuid.UUID | None,
    tax_code_id: uuid.UUID | None,
) -> SupplierRule | None:
    """Rule-quality bookkeeping at publish time (spec §6).

    When the document carried a rule suggestion: a publish whose
    confirmed values agree with everything the rule suggested counts as
    ``times_applied += 1``; any divergence (different contact, or a
    rule-suggested account/tax-code the human changed) counts as
    ``times_overridden += 1``. Returns the rule (for the change_log
    provenance / a follow-up ``update_rule``). No commit.

    ``account_id`` / ``tax_code_id`` are the *uniform* confirmed values
    across the published lines (``None`` = mixed or absent).
    """
    if doc.supplier_rule_id is None:
        return None
    rule = (
        await session.execute(
            select(SupplierRule).where(
                SupplierRule.id == doc.supplier_rule_id,
                SupplierRule.tenant_id == doc.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if rule is None:  # rule deleted since the match — nothing to score
        return None

    diverged = contact_id != rule.contact_id
    if rule.account_id is not None and account_id != rule.account_id:
        diverged = True
    if rule.tax_code_id is not None and tax_code_id != rule.tax_code_id:
        diverged = True

    if diverged:
        rule.times_overridden += 1
    else:
        rule.times_applied += 1
        rule.last_applied_at = datetime.now(UTC)
    return rule


async def learn_rule_on_publish(
    session: AsyncSession,
    doc: InboxDocument,
    *,
    company_id: uuid.UUID,
    record_kind: PublishedRecordKind | str,
    contact_id: uuid.UUID,
    account_id: uuid.UUID | None,
    tax_code_id: uuid.UUID | None,
    learn_rule: bool,
    update_rule: bool,
    matched_rule: SupplierRule | None,
) -> SupplierRule | None:
    """Publish-driven learning (spec §6).

    * ``update_rule=True`` rewrites the matched/existing rule's defaults
      to the values the human actually confirmed (and freshens the ABN
      when the document supplied one the rule lacks).
    * ``learn_rule=True`` with no existing rule for this vendor upserts
      a LEARNED rule from the confirmed values, scoped to the publish
      company (contacts are company-scoped, so a broader scope would
      suggest a foreign contact). An existing rule is never clobbered by
      ``learn_rule`` alone — rewriting defaults is ``update_rule``'s
      job.

    Returns the created/updated rule, or ``None``. No commit.
    """
    merged = merged_extract(doc)
    vendor_key = normalise_vendor_key(merged.get("vendor_name"))
    abn = normalise_abn(merged.get("vendor_abn"))
    kind = PublishedRecordKind(record_kind).value

    rule = matched_rule
    if rule is None and vendor_key is not None:
        # An existing rule the extraction-time match may have missed
        # (e.g. the reviewer fixed the vendor name in the override).
        rule = await match_supplier_rule(
            session,
            doc.tenant_id,
            vendor_abn=abn,
            vendor_name=vendor_key,
            company_id=company_id,
        )

    if rule is not None:
        if update_rule:
            rule.contact_id = contact_id
            rule.account_id = account_id
            rule.tax_code_id = tax_code_id
            rule.record_kind = kind
            if abn and not rule.vendor_abn:
                rule.vendor_abn = abn
        return rule

    if not learn_rule or vendor_key is None:
        # Nothing to learn from: no rule wanted, or no vendor identity
        # to key it on (a rule without a vendor key can never match).
        return None

    learned = SupplierRule(
        tenant_id=doc.tenant_id,
        company_id=company_id,
        vendor_key=vendor_key,
        vendor_abn=abn,
        contact_id=contact_id,
        account_id=account_id,
        tax_code_id=tax_code_id,
        record_kind=kind,
        origin=SupplierRuleOrigin.LEARNED,
        created_from_document_id=doc.id,
    )
    # SAVEPOINT-guarded insert: two near-simultaneous publishes of
    # different documents from the same new vendor both miss the match
    # above and both try to learn — ``uq_supplier_rules_scope_vendor``
    # arbitrates. The loser rolls back to the savepoint (the publish
    # transaction survives) and adopts the winner's rule instead of
    # blowing the whole publish up with a 500.
    try:
        async with session.begin_nested():
            session.add(learned)
            await session.flush()
    except IntegrityError:
        return await match_supplier_rule(
            session,
            doc.tenant_id,
            vendor_abn=abn,
            vendor_name=vendor_key,
            company_id=company_id,
        )
    return learned


async def apply_publish_rule_effects(
    session: AsyncSession,
    doc: InboxDocument,
    *,
    company_id: uuid.UUID,
    record_kind: PublishedRecordKind | str,
    contact_id: uuid.UUID,
    line_account_ids: list[uuid.UUID | None],
    line_tax_code_ids: list[uuid.UUID | None],
    learn_rule: bool,
    update_rule: bool,
) -> SupplierRule | None:
    """The one publish-time rule entry point (keeps the router thin):
    computes the uniform confirmed coding, scores the suggesting rule
    (applied/overridden), then runs learn/update. Returns the rule that
    was created or touched, if any. No commit — publish owns the txn.
    """
    account_id = _uniform(list(line_account_ids))
    tax_code_id = _uniform(list(line_tax_code_ids))
    matched = await record_publish_rule_outcome(
        session,
        doc,
        contact_id=contact_id,
        account_id=account_id,
        tax_code_id=tax_code_id,
    )
    return await learn_rule_on_publish(
        session,
        doc,
        company_id=company_id,
        record_kind=record_kind,
        contact_id=contact_id,
        account_id=account_id,
        tax_code_id=tax_code_id,
        learn_rule=learn_rule,
        update_rule=update_rule,
        matched_rule=matched,
    )


# ---------------------------------------------------------------------------
# Supplier rules — CRUD (router-facing; app-layer tenant filter everywhere)
# ---------------------------------------------------------------------------


async def list_supplier_rules(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    include_inactive: bool = False,
    company_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SupplierRule], int]:
    """Paginated tenant-scoped rule list, newest first."""
    where = [SupplierRule.tenant_id == tenant_id]
    if not include_inactive:
        where.append(SupplierRule.active.is_(True))
    if company_id is not None:
        where.append(SupplierRule.company_id == company_id)
    total = (
        await session.execute(
            select(func.count()).select_from(SupplierRule).where(*where)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(SupplierRule)
                .where(*where)
                .order_by(SupplierRule.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_supplier_rule(
    session: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID
) -> SupplierRule | None:
    return (
        await session.execute(
            select(SupplierRule).where(
                SupplierRule.id == rule_id,
                SupplierRule.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


def _coerce_record_kind(raw: Any) -> str | None:
    """Uppercased/validated record kind, or :class:`SupplierRuleError`."""
    if not raw:
        return None
    try:
        return PublishedRecordKind(str(raw).upper()).value
    except ValueError as exc:
        raise SupplierRuleError(
            f"record_kind '{raw}' is not one of EXPENSE, BILL, CREDIT_NOTE"
        ) from exc


async def validate_coding_fks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    tax_code_id: uuid.UUID | None = None,
) -> None:
    """Belt-and-braces tenant-ownership checks (RLS is the braces): a
    foreign or unknown id raises :class:`SupplierRuleError` (422)."""
    from saebooks.models.account import Account
    from saebooks.models.contact import Contact
    from saebooks.models.tax_code import TaxCode

    checks: list[tuple[str, Any, uuid.UUID]] = []
    if contact_id is not None:
        checks.append(("contact", Contact, contact_id))
    if account_id is not None:
        checks.append(("account", Account, account_id))
    if tax_code_id is not None:
        checks.append(("tax_code", TaxCode, tax_code_id))
    for label, model, value in checks:
        found = (
            await session.execute(
                select(model.id).where(
                    model.id == value, model.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise SupplierRuleError(f"{label} {value} not found")


async def create_supplier_rule(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    vendor_name: str,
    contact_id: uuid.UUID,
    company_id: uuid.UUID | None = None,
    vendor_abn: str | None = None,
    account_id: uuid.UUID | None = None,
    tax_code_id: uuid.UUID | None = None,
    record_kind: str | None = None,
) -> SupplierRule:
    """Create a MANUAL rule. ``vendor_name`` is normalised into
    ``vendor_key`` here — the service owns the normalisation. The
    partial-unique (one active rule per vendor per scope) surfaces as an
    ``IntegrityError`` the router maps to 409. No commit.
    """
    vendor_key = normalise_vendor_key(vendor_name)
    if vendor_key is None:
        raise SupplierRuleError("vendor_name must not be empty")
    abn = normalise_abn(vendor_abn) if vendor_abn else None
    if vendor_abn and abn is None:
        raise SupplierRuleError(
            "vendor_abn must contain exactly 11 digits (Australian Business Number)"
        )
    kind = _coerce_record_kind(record_kind)
    await validate_coding_fks(
        session,
        tenant_id,
        contact_id=contact_id,
        account_id=account_id,
        tax_code_id=tax_code_id,
    )
    rule = SupplierRule(
        tenant_id=tenant_id,
        company_id=company_id,
        vendor_key=vendor_key,
        vendor_abn=abn,
        contact_id=contact_id,
        account_id=account_id,
        tax_code_id=tax_code_id,
        record_kind=kind,
        origin=SupplierRuleOrigin.MANUAL,
    )
    session.add(rule)
    await session.flush()
    return rule


async def update_supplier_rule(
    session: AsyncSession,
    rule: SupplierRule,
    *,
    fields: dict[str, Any],
) -> SupplierRule:
    """Apply a PATCH-style partial update to ``rule``. Only keys present
    in ``fields`` are touched (``None`` clears a nullable column).
    Soft-delete is ``active=False``; re-activation may hit the
    partial-unique (router maps IntegrityError → 409). No commit.
    """
    if "vendor_name" in fields:
        vendor_key = normalise_vendor_key(fields["vendor_name"])
        if vendor_key is None:
            raise SupplierRuleError("vendor_name must not be empty")
        rule.vendor_key = vendor_key
    if "vendor_abn" in fields:
        raw = fields["vendor_abn"]
        if raw is None:
            rule.vendor_abn = None
        else:
            abn = normalise_abn(raw)
            if abn is None:
                raise SupplierRuleError(
                    "vendor_abn must contain exactly 11 digits "
                    "(Australian Business Number)"
                )
            rule.vendor_abn = abn
    if "record_kind" in fields:
        rule.record_kind = _coerce_record_kind(fields["record_kind"])
    await validate_coding_fks(
        session,
        rule.tenant_id,
        contact_id=fields.get("contact_id"),
        account_id=fields.get("account_id"),
        tax_code_id=fields.get("tax_code_id"),
    )
    if "contact_id" in fields:
        if fields["contact_id"] is None:
            raise SupplierRuleError("contact_id is required on a supplier rule")
        rule.contact_id = fields["contact_id"]
    if "account_id" in fields:
        rule.account_id = fields["account_id"]
    if "tax_code_id" in fields:
        rule.tax_code_id = fields["tax_code_id"]
    if "company_id" in fields:
        rule.company_id = fields["company_id"]
    if "active" in fields and fields["active"] is not None:
        rule.active = bool(fields["active"])
    await session.flush()
    return rule


# ---------------------------------------------------------------------------
# Email-in addresses (spec §4, phase 3) — the address IS the credential
# ---------------------------------------------------------------------------

# 10 random bytes → 16 lowercase base32 chars (spec minimum is 12+),
# 80 bits of entropy. No padding at this length.
_EMAIL_TOKEN_BYTES = 10


def mint_email_token() -> str:
    """Server-minted routing token: lowercase base32 (RFC 4648 alphabet
    lowercased → ``[a-z2-7]``), 16 characters, unguessable. The full
    ingestion address is ``<token>@<SAEBOOKS_INBOX_MAIL_DOMAIN>``."""
    raw = secrets.token_bytes(_EMAIL_TOKEN_BYTES)
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


async def list_email_addresses(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    include_revoked: bool = False,
) -> list[InboxEmailAddress]:
    """The tenant's ingestion addresses, newest first."""
    where = [InboxEmailAddress.tenant_id == tenant_id]
    if not include_revoked:
        where.append(InboxEmailAddress.active.is_(True))
    rows = (
        await session.execute(
            select(InboxEmailAddress)
            .where(*where)
            .order_by(InboxEmailAddress.created_at.desc())
        )
    ).scalars().all()
    return list(rows)


async def get_email_address(
    session: AsyncSession, tenant_id: uuid.UUID, address_id: uuid.UUID
) -> InboxEmailAddress | None:
    return (
        await session.execute(
            select(InboxEmailAddress).where(
                InboxEmailAddress.id == address_id,
                InboxEmailAddress.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


async def create_email_address(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    company_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> InboxEmailAddress:
    """Mint a new ingestion address. Multiple ACTIVE addresses per
    tenant are the design (one per company on a multi-entity tenant).
    Retries the astronomically-unlikely global token collision. No
    commit."""
    for _ in range(3):
        addr = InboxEmailAddress(
            tenant_id=tenant_id,
            company_id=company_id,
            token=mint_email_token(),
            created_by=created_by,
        )
        session.add(addr)
        try:
            async with session.begin_nested():
                await session.flush()
            return addr
        except IntegrityError as exc:  # pragma: no cover — 2^-80 event
            if "token" not in str(exc.orig):
                raise
            session.expunge(addr)
    raise RuntimeError("could not mint a unique inbox email token")


def revoke_email_address(address: InboxEmailAddress) -> InboxEmailAddress:
    """Soft revoke — the token stops routing (the poll enumerator only
    returns active rows; mail to it quarantines), the row stays as the
    audit record. Idempotent. No commit."""
    if address.active:
        address.active = False
        address.revoked_at = datetime.now(UTC)
    return address


# ---------------------------------------------------------------------------
# Ingest funnel (spec §5 — one funnel, every surface)
# ---------------------------------------------------------------------------


async def _find_active_duplicate(
    session: AsyncSession, tenant_id: uuid.UUID, sha256: str
) -> InboxDocument | None:
    """Return the live row holding this content hash, if any.

    Mirrors the partial-unique index predicate exactly:
    ``(tenant_id, sha256) WHERE status NOT IN ('REJECTED','DUPLICATE')``
    — a mistakenly rejected document stays recoverable by re-upload.
    """
    stmt = (
        select(InboxDocument)
        .where(
            InboxDocument.tenant_id == tenant_id,
            InboxDocument.sha256 == sha256,
            InboxDocument.status.notin_(
                [_S.REJECTED.value, _S.DUPLICATE.value]
            ),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def count_blob_siblings(
    session: AsyncSession, doc: InboxDocument
) -> int:
    """Other inbox rows (same tenant) sharing ``doc``'s vault blob.

    Emailed byte-duplicates reuse the original's ``vault_file_id``
    (:func:`ingest_email_attachment` stores no second copy) — reject
    must not archive a blob that live sibling rows still reference.
    """
    stmt = (
        select(func.count())
        .select_from(InboxDocument)
        .where(
            InboxDocument.tenant_id == doc.tenant_id,
            InboxDocument.vault_file_id == doc.vault_file_id,
            InboxDocument.id != doc.id,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def ingest(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    data: bytes,
    filename: str,
    mime: str,
    source: InboxDocumentSource | str,
    company_id: uuid.UUID | None = None,
    source_ref: str | None = None,
    actor: str | None = None,
    created_by: uuid.UUID | None = None,
    extract_enabled: bool = True,
) -> tuple[InboxDocument, bool]:
    """The one capture funnel. Returns ``(document, duplicate)``.

    ``duplicate=True`` means the bytes were already in the inbox: the
    returned row is the *existing* live document and nothing new was
    stored (a fresh blob uploaded during a lost race is soft-archived).
    The router turns this into ``200 + {"duplicate": true}`` — a mobile
    double-tap must never read as failure.

    ``extract_enabled=False`` (``FLAG_AI_EXTRACTION`` off — Community /
    Offline editions) skips the model entirely: the document lands in
    ``NEEDS_REVIEW`` empty, ready for manual keying.

    Vault errors from the initial upload propagate to the caller (the
    blob-durability guarantee failed, so there is nothing to keep);
    extraction-stage failures never propagate — see :func:`run_extraction`.
    """
    digest = hashlib.sha256(data).hexdigest()

    # Dedupe pre-check — cheap read before we touch the vault.
    existing = await _find_active_duplicate(session, tenant_id, digest)
    if existing is not None:
        return existing, True

    # Blob first: durable in the vault before any extraction attempt.
    meta = await vault_client.upload(
        tenant_id,
        file=data,
        filename=filename,
        content_type=mime,
        actor=actor,
    )
    vault_file_id = uuid.UUID(meta["id"])

    doc = InboxDocument(
        tenant_id=tenant_id,
        company_id=company_id,
        vault_file_id=vault_file_id,
        sha256=digest,
        filename=filename[:255],
        mime=mime[:100],
        size_bytes=len(data),
        source=InboxDocumentSource(source),
        source_ref=source_ref,
        status=_S.RECEIVED,
        created_by=created_by,
    )
    session.add(doc)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        # Race backstop: a concurrent request inserted the same
        # (tenant, sha256) between our pre-check and our flush — the
        # partial-unique index is the arbiter. Soft-archive the blob we
        # just uploaded (best-effort; an unlinked archived blob is an
        # accepted state) and hand back the winner's row.
        await session.rollback()
        with contextlib.suppress(vault_client.VaultError):
            await vault_client.delete(tenant_id, vault_file_id)
        existing = await _find_active_duplicate(session, tenant_id, digest)
        if existing is not None:
            return existing, True
        raise  # not the sha256 unique (e.g. source_ref replay) — surface it

    if not extract_enabled:
        # Flag off → skip the model, straight to manual keying.
        transition(doc, _S.EXTRACTING)
        transition(doc, _S.NEEDS_REVIEW)
        await session.commit()
        # Load server-generated columns (created_at / updated_at) so the
        # caller can serialise without a sync lazy-load (asyncpg).
        await session.refresh(doc)
        return doc, False

    doc = await run_extraction(session, doc)
    return doc, False


async def ingest_email_attachment(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    data: bytes,
    filename: str,
    mime: str,
    source_ref: str,
    company_id: uuid.UUID | None = None,
    actor: str | None = None,
) -> tuple[InboxDocument, str]:
    """Per-attachment ingest for the mail poller (spec §4).

    Returns ``(document, outcome)`` with outcome one of:

    * ``"REPLAY"`` — this ``(tenant, EMAIL, source_ref)`` already has a
      row (a crashed run completed this attachment); nothing stored.
    * ``"DUPLICATE"`` — the bytes are already live in the inbox under a
      different source; a **DUPLICATE row** is created pointing at the
      original (``duplicate_of_id`` + the original's blob — no second
      blob is stored) so the inbox shows "already sent". This differs
      from the upload surface deliberately: an upload double-tap returns
      the existing row, an emailed re-send leaves an audit row.
    * ``"INGESTED"`` — blob stored (durable first), fresh RECEIVED row;
      extraction is deferred to the cron sweep (spec §5) — the poller
      never blocks on the model.

    No commit — the message walk owns transaction boundaries. The
    partial uniques (``source_ref`` replay guard, ``sha256`` dedupe) are
    the race backstops for overlapping poller runs; an
    ``IntegrityError`` out of the caller's commit means another run won
    the message and the whole message replays safely.
    """
    existing_ref = (
        await session.execute(
            select(InboxDocument).where(
                InboxDocument.tenant_id == tenant_id,
                InboxDocument.source == InboxDocumentSource.EMAIL.value,
                InboxDocument.source_ref == source_ref,
            )
        )
    ).scalars().first()
    if existing_ref is not None:
        return existing_ref, "REPLAY"

    digest = hashlib.sha256(data).hexdigest()
    original = await _find_active_duplicate(session, tenant_id, digest)
    if original is not None:
        dup = InboxDocument(
            tenant_id=tenant_id,
            company_id=company_id,
            vault_file_id=original.vault_file_id,
            sha256=digest,
            filename=filename[:255],
            mime=mime[:100],
            size_bytes=len(data),
            source=InboxDocumentSource.EMAIL,
            source_ref=source_ref,
            status=_S.DUPLICATE,
            duplicate_of_id=original.id,
        )
        session.add(dup)
        await session.flush()
        return dup, "DUPLICATE"

    meta = await vault_client.upload(
        tenant_id,
        file=data,
        filename=filename,
        content_type=mime,
        actor=actor or "saebooks:inbox-poll-mail",
    )
    doc = InboxDocument(
        tenant_id=tenant_id,
        company_id=company_id,
        vault_file_id=uuid.UUID(meta["id"]),
        sha256=digest,
        filename=filename[:255],
        mime=mime[:100],
        size_bytes=len(data),
        source=InboxDocumentSource.EMAIL,
        source_ref=source_ref,
        status=_S.RECEIVED,
    )
    session.add(doc)
    await session.flush()
    return doc, "INGESTED"


async def count_email_documents_today(
    session: AsyncSession, tenant_id: uuid.UUID
) -> int:
    """EMAIL-sourced documents created so far this UTC day — the
    per-tenant abuse-quota input (spec §4)."""
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        await session.execute(
            select(func.count())
            .select_from(InboxDocument)
            .where(
                InboxDocument.tenant_id == tenant_id,
                InboxDocument.source == InboxDocumentSource.EMAIL.value,
                InboxDocument.created_at >= day_start,
            )
        )
    ).scalar_one()


async def run_extraction(
    session: AsyncSession, doc: InboxDocument
) -> InboxDocument:
    """Run (or re-run) extraction for ``doc`` synchronously.

    Owns the RECEIVED → EXTRACTING → {NEEDS_REVIEW | READY | RECEIVED |
    FAILED} leg of the machine and commits its outcome. Raises
    :class:`IllegalTransitionError` when ``doc`` is in a terminal
    status; everything else is absorbed into document state — the
    caller's request never 5xxes because the brain was down.
    """
    transition(doc, _S.EXTRACTING)
    doc.claimed_at = datetime.now(UTC)
    await session.commit()
    return await _extract_claimed(session, doc)


async def _extract_claimed(
    session: AsyncSession, doc: InboxDocument
) -> InboxDocument:
    """The post-claim extraction body — shared by the interactive path
    (:func:`run_extraction`) and the cron sweep
    (:func:`sweep_process_claimed`): ``doc`` is already EXTRACTING with
    ``claimed_at`` stamped and committed. Extraction is idempotent, so
    the two paths (and overlapping cron fires) are mutually safe.
    """
    started = time.monotonic()

    # Pull the bytes back from the vault — the engine keeps none.
    try:
        data, _mime, _fn = await vault_client.download(
            doc.tenant_id, doc.vault_file_id
        )
    except vault_client.VaultError as exc:
        logger.warning(
            "inbox extraction: vault download failed for %s: %s", doc.id, exc
        )
        return await _extraction_transport_failure(session, doc, f"vault: {exc}")

    try:
        result = await ai_extraction.extract_document(data, doc.mime)
    except Exception as exc:  # transport/config failure by contract
        # extract_document soft-catches model/API errors internally; an
        # exception here is transport-shaped (LiteLLM unconfigured,
        # unexpected client bug). Back to RECEIVED for the sweep/retry.
        logger.warning("inbox extraction transport failure for %s: %s", doc.id, exc)
        return await _extraction_transport_failure(session, doc, str(exc))

    doc.attempt_count = (doc.attempt_count or 0) + 1
    doc.extracted_at = datetime.now(UTC)
    doc.claimed_at = None
    doc.last_error = None
    # Verbatim model output — re-extraction replaces it wholesale
    # (idempotent); reviewer edits never land here.
    doc.extract = result
    # The model id lives module-private in ai_extraction; surfacing it
    # here is provenance, not behaviour, so the private read is accepted.
    doc.extract_model = getattr(ai_extraction, "_MODEL", None)

    # Supplier-rule matching (phase 2): ABN-exact → vendor_key-exact,
    # suggestion-only. Idempotent like extraction itself.
    await _apply_rule_matching(session, doc)

    error = result.get("extraction_error")
    if error:
        # Model soft-failure: the call answered but the output is
        # unusable/partial. Reviewable immediately, marked PARTIAL.
        doc.extraction_confidence = ExtractionConfidence.PARTIAL
        doc.extraction_error = str(error)[:2000]
        transition(doc, _S.NEEDS_REVIEW)
    else:
        doc.extraction_confidence = ExtractionConfidence.OK
        doc.extraction_error = None
        transition(doc, _S.READY if is_ready(doc) else _S.NEEDS_REVIEW)

    await session.commit()
    await session.refresh(doc)  # reload server-touched columns for serialisation
    _log_transition(
        doc,
        "EXTRACTING",
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    # Advisory near-duplicate flag (spec §6/§9, phase 4) — read-only,
    # after the extraction outcome is committed. Logged for the Loki
    # stack; the detail/stats responses recompute it live so the banner
    # stays current as siblings publish or reject.
    try:
        advisory = await find_advisory_duplicates(session, doc)
    except Exception:  # pragma: no cover — advisory must never break extraction
        logger.exception("inbox advisory duplicate check failed for %s", doc.id)
        advisory = []
    if advisory:
        logger.info(
            "inbox advisory duplicate: doc_id=%s tenant=%s matches=%s",
            doc.id,
            doc.tenant_id,
            ",".join(str(d.id) for d in advisory),
        )
    return doc


async def _extraction_transport_failure(
    session: AsyncSession, doc: InboxDocument, error: str
) -> InboxDocument:
    """Transport failure (LiteLLM down, vault unreachable): back to
    RECEIVED with ``last_error`` and the spec §5 exponential backoff
    (``next_attempt_at = now + 60s·5^(attempt_count−1)``); **FAILED
    after :data:`SWEEP_MAX_ATTEMPTS`** — still visible, still
    hand-keyable. Upload/capture still succeeds either way."""
    doc.attempt_count = (doc.attempt_count or 0) + 1
    doc.claimed_at = None
    doc.last_error = error[:2000]
    if doc.attempt_count >= SWEEP_MAX_ATTEMPTS:
        transition(doc, _S.FAILED)
    else:
        doc.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=sweep_backoff_delay_s(doc.attempt_count)
        )
        transition(doc, _S.RECEIVED)
    await session.commit()
    await session.refresh(doc)  # reload server-touched columns for serialisation
    _log_transition(doc, "EXTRACTING")
    return doc


# ---------------------------------------------------------------------------
# Cron sweep (spec §5, phase 3) — the guarantee behind the interactive path
# ---------------------------------------------------------------------------

SWEEP_CLAIM_BATCH = 10
SWEEP_MAX_ATTEMPTS = 5
SWEEP_BACKOFF_BASE_S = 60
SWEEP_BACKOFF_FACTOR = 5
SWEEP_RECLAIM_AFTER_S = 600  # EXTRACTING older than 10 min is reclaimed


def sweep_backoff_delay_s(attempt_count: int) -> int:
    """Spec §5 schedule: ``60s·5^(n−1)`` — 60s, 300s, 1500s, 7500s."""
    return SWEEP_BACKOFF_BASE_S * SWEEP_BACKOFF_FACTOR ** (max(attempt_count, 1) - 1)


def _log_transition(
    doc: InboxDocument,
    from_status: str,
    *,
    duration_ms: int | None = None,
) -> None:
    """The structured per-transition log line (spec §5 observability):
    ``doc_id, tenant, from→to, attempt, duration_ms, model`` — one line
    per state change for the Loki stack to index."""
    logger.info(
        "inbox transition: doc_id=%s tenant=%s from=%s to=%s attempt=%d "
        "duration_ms=%s model=%s",
        doc.id,
        doc.tenant_id,
        from_status,
        str(doc.status),
        doc.attempt_count or 0,
        duration_ms if duration_ms is not None else "-",
        doc.extract_model or "-",
    )


async def sweep_reclaim(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    older_than_s: int = SWEEP_RECLAIM_AFTER_S,
) -> int:
    """Reclaim EXTRACTING rows whose claim is older than 10 minutes —
    a crashed worker/request left them mid-flight. Extraction is
    idempotent so putting them back to RECEIVED (keeping their
    ``next_attempt_at``) is safe. Commits; returns the reclaim count."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
    rows = (
        (
            await session.execute(
                select(InboxDocument)
                .where(
                    InboxDocument.tenant_id == tenant_id,
                    InboxDocument.status == _S.EXTRACTING.value,
                    InboxDocument.claimed_at.is_not(None),
                    InboxDocument.claimed_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for doc in rows:
        transition(doc, _S.RECEIVED)
        doc.claimed_at = None
        _log_transition(doc, "EXTRACTING")
    await session.commit()
    return len(rows)


async def sweep_claim(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    batch: int = SWEEP_CLAIM_BATCH,
) -> list[uuid.UUID]:
    """Claim up to ``batch`` due documents (spec §5): ``status='RECEIVED'
    AND next_attempt_at <= now() ORDER BY next_attempt_at LIMIT batch
    FOR UPDATE SKIP LOCKED`` — overlapping cron fires and the
    interactive path never double-claim. Each claimed row is stamped
    EXTRACTING + ``claimed_at`` and committed (a crash mid-batch leaves
    claims visible and reclaimable after 10 minutes)."""
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(InboxDocument)
                .where(
                    InboxDocument.tenant_id == tenant_id,
                    InboxDocument.status == _S.RECEIVED.value,
                    InboxDocument.next_attempt_at <= now,
                )
                .order_by(InboxDocument.next_attempt_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claimed: list[uuid.UUID] = []
    for doc in rows:
        transition(doc, _S.EXTRACTING)
        doc.claimed_at = now
        claimed.append(doc.id)
        _log_transition(doc, "RECEIVED")
    await session.commit()
    return claimed


async def sweep_process_claimed(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    extract_enabled: bool = True,
) -> InboxDocument | None:
    """Process one claimed document. Returns ``None`` when the claim is
    gone (another worker finished it, or it was reclaimed) — never an
    error, per the mutual-safety contract.

    ``extract_enabled=False`` (``FLAG_AI_EXTRACTION`` off on this
    edition) skips the model and lands the document in NEEDS_REVIEW
    empty for manual keying — the same degradation as the upload path.
    """
    doc = (
        await session.execute(
            select(InboxDocument).where(
                InboxDocument.id == doc_id,
                InboxDocument.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None or InboxDocumentStatus(doc.status) is not _S.EXTRACTING:
        return None
    if not extract_enabled:
        doc.claimed_at = None
        transition(doc, _S.NEEDS_REVIEW)
        await session.commit()
        await session.refresh(doc)
        _log_transition(doc, "EXTRACTING")
        return doc
    return await _extract_claimed(session, doc)
