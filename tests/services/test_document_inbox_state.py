"""Unit tests for the Document Inbox state machine + completeness rules.

Pure-Python — no DB, no vault, no model calls. The transition matrix is
spec §6 (issue #33) plus the one documented extension: EXTRACTING is
reachable from every reviewable state (the manual-retry path), not just
RECEIVED.
"""
from __future__ import annotations

import pytest

from saebooks.models.inbox_document import InboxDocument
from saebooks.models.inbox_document import InboxDocumentStatus as S
from saebooks.services import document_inbox as svc


def _doc(status: S = S.NEEDS_REVIEW, **kw) -> InboxDocument:
    doc = InboxDocument(status=status)
    for key, value in kw.items():
        setattr(doc, key, value)
    return doc


# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------

_LEGAL: list[tuple[S, S]] = [
    (S.RECEIVED, S.EXTRACTING),
    (S.EXTRACTING, S.NEEDS_REVIEW),
    (S.EXTRACTING, S.READY),
    (S.EXTRACTING, S.RECEIVED),
    (S.EXTRACTING, S.FAILED),
    (S.NEEDS_REVIEW, S.READY),
    (S.READY, S.NEEDS_REVIEW),
    (S.NEEDS_REVIEW, S.PUBLISHED),
    (S.NEEDS_REVIEW, S.REJECTED),
    (S.READY, S.PUBLISHED),
    (S.READY, S.REJECTED),
    (S.FAILED, S.PUBLISHED),
    (S.FAILED, S.REJECTED),
    # Manual-retry extension (documented in services/document_inbox.py).
    (S.NEEDS_REVIEW, S.EXTRACTING),
    (S.READY, S.EXTRACTING),
    (S.FAILED, S.EXTRACTING),
]


@pytest.mark.parametrize(("current", "target"), _LEGAL)
def test_legal_transitions(current: S, target: S) -> None:
    doc = _doc(current)
    svc.transition(doc, target)
    assert doc.status == target


def test_every_other_pair_is_illegal() -> None:
    legal = set(_LEGAL)
    for current in S:
        for target in S:
            if current == target or (current, target) in legal:
                continue
            with pytest.raises(svc.IllegalTransitionError):
                svc.ensure_can_transition(current, target)


@pytest.mark.parametrize("terminal", [S.PUBLISHED, S.REJECTED, S.DUPLICATE])
def test_terminal_states_have_no_exits(terminal: S) -> None:
    for target in S:
        if target == terminal:
            continue
        with pytest.raises(svc.IllegalTransitionError):
            svc.ensure_can_transition(terminal, target)


def test_transition_accepts_plain_strings() -> None:
    """DB rows come back with TEXT statuses — the helper must not
    depend on enum instances."""
    doc = _doc("RECEIVED")
    svc.transition(doc, "EXTRACTING")
    assert doc.status == S.EXTRACTING


def test_illegal_transition_error_carries_states() -> None:
    with pytest.raises(svc.IllegalTransitionError) as excinfo:
        svc.ensure_can_transition(S.PUBLISHED, S.REJECTED)
    assert excinfo.value.current == S.PUBLISHED
    assert excinfo.value.target == S.REJECTED
    assert "PUBLISHED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Merged view + completeness
# ---------------------------------------------------------------------------


_COMPLETE_OVERRIDE = {
    "contact_id": "11111111-1111-1111-1111-111111111111",
    "total": "110.00",
    "line_items": [
        {
            "description": "Fuel",
            "account_id": "22222222-2222-2222-2222-222222222222",
            "tax_code_id": "33333333-3333-3333-3333-333333333333",
            "unit_price": "100.00",
        }
    ],
}


def test_merged_extract_override_wins() -> None:
    doc = _doc(
        extract={"vendor_name": "BP", "total": "99.00", "line_items": [{"a": 1}]},
        extraction_override={"total": "110.00", "line_items": [{"b": 2}]},
    )
    merged = svc.merged_extract(doc)
    assert merged["vendor_name"] == "BP"  # extract survives
    assert merged["total"] == "110.00"  # override wins
    assert merged["line_items"] == [{"b": 2}]  # lines replaced wholesale


def test_is_ready_true_when_fully_coded() -> None:
    doc = _doc(extract={"total": "110.00"}, extraction_override=_COMPLETE_OVERRIDE)
    assert svc.is_ready(doc)


@pytest.mark.parametrize(
    "mutation",
    [
        {"contact_id": None},
        {"total": None},
        {"total": ""},
        {"line_items": []},
        {"line_items": [{"description": "x", "account_id": "a"}]},  # no tax_code_id
        {"line_items": [{"description": "x", "tax_code_id": "t"}]},  # no account_id
    ],
)
def test_is_ready_false_when_anything_missing(mutation: dict) -> None:
    override = {**_COMPLETE_OVERRIDE, **mutation}
    doc = _doc(extraction_override=override)
    assert not svc.is_ready(doc)


def test_is_ready_false_on_empty_document() -> None:
    assert not svc.is_ready(_doc())


def test_recompute_promotes_needs_review_to_ready() -> None:
    doc = _doc(S.NEEDS_REVIEW, extraction_override=_COMPLETE_OVERRIDE)
    svc.recompute_completeness(doc)
    assert doc.status == S.READY


def test_recompute_demotes_ready_to_needs_review() -> None:
    doc = _doc(S.READY, extraction_override={"contact_id": None})
    svc.recompute_completeness(doc)
    assert doc.status == S.NEEDS_REVIEW


@pytest.mark.parametrize(
    "status", [S.RECEIVED, S.EXTRACTING, S.FAILED, S.PUBLISHED, S.REJECTED, S.DUPLICATE]
)
def test_recompute_is_noop_outside_review_states(status: S) -> None:
    doc = _doc(status, extraction_override=_COMPLETE_OVERRIDE)
    svc.recompute_completeness(doc)
    assert doc.status == status
