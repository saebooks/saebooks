"""Unit tests for the runtime circuit breaker primitive (M2 wave 2a, P0a).

Pure state-machine tests against ``saebooks.services.circuit_breaker`` — no
DB, no HTTP, no ``grpc_gen`` dependency. An injected ``clock`` callable
stands in for wall-clock time so cooldown/half-open transitions are tested
deterministically without real ``sleep()`` calls. Per the wave-1 lesson,
assertions are on observable behaviour (``allow_request()`` return values,
``state``, ``consecutive_failures``) — never on framework/library internals.
"""
from __future__ import annotations

import pytest

from saebooks.services.circuit_breaker import BreakerState, CircuitBreaker


class _FakeClock:
    """A settable monotonic clock for deterministic cooldown tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


def _breaker(clock: _FakeClock, *, threshold: int = 3, cooldown: float = 10.0) -> CircuitBreaker:
    return CircuitBreaker(
        "test", failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock
    )


# --------------------------------------------------------------------------- #
# Construction validation                                                      #
# --------------------------------------------------------------------------- #
def test_rejects_invalid_thresholds(clock: _FakeClock) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker("x", failure_threshold=0, clock=clock)
    with pytest.raises(ValueError):
        CircuitBreaker("x", cooldown_seconds=-1, clock=clock)


# --------------------------------------------------------------------------- #
# CLOSED -> OPEN after N consecutive failures                                  #
# --------------------------------------------------------------------------- #
def test_starts_closed_and_allows_requests(clock: _FakeClock) -> None:
    b = _breaker(clock)
    assert b.state is BreakerState.CLOSED
    assert b.allow_request() is True


def test_trips_open_after_n_consecutive_failures(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=3)
    b.record_failure()
    assert b.state is BreakerState.CLOSED
    b.record_failure()
    assert b.state is BreakerState.CLOSED
    b.record_failure()
    assert b.state is BreakerState.OPEN


def test_success_resets_the_consecutive_counter(clock: _FakeClock) -> None:
    """N-1 failures then a success must NOT trip the breaker on the next
    single failure — the counter is CONSECUTIVE, not cumulative."""
    b = _breaker(clock, threshold=3)
    b.record_failure()
    b.record_failure()
    b.record_success()
    assert b.state is BreakerState.CLOSED
    assert b.consecutive_failures == 0
    b.record_failure()
    assert b.state is BreakerState.CLOSED  # only 1 consecutive failure now


# --------------------------------------------------------------------------- #
# OPEN — fail fast, no probe until cooldown elapses                            #
# --------------------------------------------------------------------------- #
def test_open_fails_fast_until_cooldown_elapses(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    assert b.state is BreakerState.OPEN
    assert b.allow_request() is False  # immediately after trip
    clock.advance(5.0)
    assert b.allow_request() is False  # still within cooldown
    assert b.state is BreakerState.OPEN  # unchanged by a denied probe


def test_cooldown_elapsed_grants_single_half_open_probe(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    clock.advance(10.0)
    assert b.allow_request() is True  # the probe
    assert b.state is BreakerState.HALF_OPEN
    # A second concurrent caller must NOT also get a probe slot.
    assert b.allow_request() is False


# --------------------------------------------------------------------------- #
# HALF_OPEN -> CLOSED on success / -> OPEN on failure                          #
# --------------------------------------------------------------------------- #
def test_half_open_success_closes_breaker(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    clock.advance(10.0)
    assert b.allow_request() is True
    b.record_success()
    assert b.state is BreakerState.CLOSED
    assert b.consecutive_failures == 0
    assert b.allow_request() is True


def test_half_open_failure_reopens_breaker_with_fresh_cooldown(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    clock.advance(10.0)
    assert b.allow_request() is True  # probe granted
    b.record_failure()
    assert b.state is BreakerState.OPEN
    # Fresh cooldown clock started at the probe failure, not the original trip.
    assert b.allow_request() is False
    clock.advance(9.9)
    assert b.allow_request() is False
    clock.advance(0.2)
    assert b.allow_request() is True


def test_half_open_self_heals_if_probe_never_reports(clock: _FakeClock) -> None:
    """A granted probe whose caller never calls record_success/failure (e.g.
    an unrelated exception, a cancelled task) must not wedge the breaker
    HALF_OPEN forever — after another full cooldown window a fresh probe is
    granted."""
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    clock.advance(10.0)
    assert b.allow_request() is True  # probe #1 granted, never resolved
    assert b.state is BreakerState.HALF_OPEN
    clock.advance(5.0)
    assert b.allow_request() is False  # still within the stale-probe window
    clock.advance(5.1)
    assert b.allow_request() is True  # self-healed: a fresh probe is granted
    assert b.state is BreakerState.HALF_OPEN


# --------------------------------------------------------------------------- #
# reset()                                                                       #
# --------------------------------------------------------------------------- #
def test_reset_forces_closed(clock: _FakeClock) -> None:
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    assert b.state is BreakerState.OPEN
    b.reset()
    assert b.state is BreakerState.CLOSED
    assert b.consecutive_failures == 0
    assert b.allow_request() is True
