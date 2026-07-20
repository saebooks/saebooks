"""Pure state-machine transition tests for the EE KMD3 filing lifecycle.

No DB, no network — just ``advance(state, event)``. Proves every legal
transition and that illegal ones fail loud with ``EEFilingStateError``.
"""
from __future__ import annotations

import pytest

from saebooks.services.lodgement.adapters.ee_client import (
    EEFilingEvent,
    EEFilingState,
    advance,
)
from saebooks.services.lodgement.exceptions import EEFilingStateError

S = EEFilingState
E = EEFilingEvent


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (S.IDLE, E.SUBMIT, S.SUBMITTED),
        (S.SUBMITTED, E.POLL_PENDING, S.PENDING),
        (S.SUBMITTED, E.POLL_ACCEPTED, S.ACCEPTED),
        (S.SUBMITTED, E.POLL_REJECTED, S.REJECTED),
        (S.PENDING, E.POLL_PENDING, S.PENDING),
        (S.PENDING, E.POLL_ACCEPTED, S.ACCEPTED),
        (S.PENDING, E.POLL_REJECTED, S.REJECTED),
        (S.ACCEPTED, E.CONFIRM, S.CONFIRMED),
    ],
)
def test_legal_transitions(state: S, event: E, expected: S) -> None:
    assert advance(state, event) is expected


def test_full_happy_path() -> None:
    st = S.IDLE
    st = advance(st, E.SUBMIT)
    assert st is S.SUBMITTED
    st = advance(st, E.POLL_PENDING)
    assert st is S.PENDING
    st = advance(st, E.POLL_ACCEPTED)
    assert st is S.ACCEPTED
    st = advance(st, E.CONFIRM)
    assert st is S.CONFIRMED


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (S.IDLE, E.POLL_PENDING),   # cannot poll before submit
        (S.IDLE, E.CONFIRM),        # cannot confirm before submit
        (S.SUBMITTED, E.SUBMIT),    # cannot re-submit in-flight
        (S.SUBMITTED, E.CONFIRM),   # cannot confirm before accepted
        (S.PENDING, E.CONFIRM),     # cannot confirm while pending
        (S.REJECTED, E.POLL_PENDING),   # terminal
        (S.REJECTED, E.CONFIRM),        # terminal
        (S.CONFIRMED, E.POLL_ACCEPTED),  # terminal
        (S.CONFIRMED, E.SUBMIT),         # terminal
    ],
)
def test_illegal_transitions_raise(state: S, event: E) -> None:
    with pytest.raises(EEFilingStateError, match="illegal filing transition"):
        advance(state, event)


def test_state_values_are_persistence_tokens() -> None:
    """The enum ``.value`` strings ARE the ee_filing_state column tokens."""
    assert S.SUBMITTED.value == "submitted"
    assert S.PENDING.value == "pending"
    assert S.ACCEPTED.value == "accepted"
    assert S.REJECTED.value == "rejected"
    assert S.CONFIRMED.value == "confirmed"
    # All fit the String(16) column.
    assert all(len(s.value) <= 16 for s in S)
