"""Runtime circuit breaker for delegated-module clients (M2 wave 2a, P0a).

Context — audit doc ``m2-module-architecture-audit-2026-07-09.md`` §4 Layer C
/ §7.1 decision 2: Richard decided to deploy ``capture`` / ``preaccounting`` /
``platform`` as LIVE separate containers in M2. The audit's own warning is
that delegating without a runtime breaker is *worse than the monolith* — a
down module gets hammered by every request instead of degrading. This module
is the shared primitive; ``capture_client.py`` / ``preaccounting_client.py``
/ ``platform_client.py`` each own one instance and consult it inside their
existing ``delegating()`` gate (see those modules for the wiring).

Design
------
Deliberately SYNCHRONOUS — no ``asyncio.Lock``. ``delegating()`` is a plain
``def`` called synchronously at ~60 call sites across the engine (and by
``test_delegating_reflects_flag`` in three existing test files); making the
breaker's methods ``async`` would force ``delegating()`` to become ``async``
and cascade ``await`` into every call site for no benefit — on a single
asyncio event loop a sync method with no ``await`` inside it already runs to
completion without yielding, so its state read+mutate is atomic with respect
to other coroutines. A ``threading.Lock`` is still used so the breaker is
also safe if ever called from a worker thread (e.g. the gRPC server's thread
pool), which the asyncio guarantee above does not cover.

State machine
-------------
* **CLOSED** — normal operation. ``allow_request()`` always True.
  ``record_failure()`` increments a *consecutive*-failure counter; reaching
  ``failure_threshold`` trips the breaker OPEN. ``record_success()`` resets
  the counter to 0 (this is what makes the threshold "N consecutive", not
  "N total").
* **OPEN** — ``allow_request()`` returns False (fail fast, no network call
  attempted — this is the "don't hammer the down service" behaviour) until
  ``cooldown_seconds`` have elapsed since the trip. The NEXT caller to ask
  after that is granted a single HALF_OPEN probe — the state flips to
  HALF_OPEN as a side effect of that one ``allow_request()`` call, so every
  other concurrent caller in the meantime still sees False.
* **HALF_OPEN** — exactly one probe is (believed to be) in flight.
  ``record_success()`` closes the breaker. ``record_failure()`` reopens it
  with a fresh cooldown clock. If the probe caller never reports an outcome
  (an exception unrelated to the transport call, a cancelled task), the
  half-open slot would wedge the breaker open forever — so
  ``allow_request()`` also self-heals: a HALF_OPEN state older than
  ``cooldown_seconds`` is treated as a fresh probe opportunity.

Only transport-boundary failures (connection refused, timeout — NOT a 4xx/5xx
HTTP response the module deliberately returned, e.g. a 409 version conflict
or 422 domain-validation error) should call ``record_failure()``. Feeding
ordinary application errors into the breaker would trip a healthy module
offline on ordinary traffic. See the ``record_failure()``/``record_success()``
call sites in the three ``*_client.py`` modules.

Deliberately NOT wired to ``platform_client``'s boot-time
``verify_key_parity_or_disable()`` kill-switch — that is a one-way,
boot-permanent "the module cannot mint engine-verifiable tokens" decision;
this breaker is a runtime-transient, self-healing "the module is
unreachable right now" signal. Keep the two separate.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("saebooks.circuit_breaker")


class BreakerState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class DelegatedServiceError(RuntimeError):
    """Base class for delegated-module client errors.

    ``CaptureServiceError`` / ``PreAccountingServiceError`` /
    ``PlatformServiceError`` all subclass this so a SINGLE exception handler
    (``saebooks.api.errors``) maps any delegated-module failure to a
    structured 503, keyed off the ``module`` class attribute each subclass
    sets.
    """

    module: str = "delegated"


class CircuitBreaker:
    """Per-module runtime breaker: trip-open after N consecutive failures,
    cooldown, single-probe half-open. See module docstring for the state
    machine. Thread-safe (``threading.Lock``); intentionally not ``async``.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        # Reused as "opened at" (OPEN) and "probe granted at" (HALF_OPEN).
        self._transitioned_at: float | None = None

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def allow_request(self) -> bool:
        """True if a delegated call should be attempted right now.

        Mutates state: consumes the single HALF_OPEN probe slot, or
        transitions OPEN -> HALF_OPEN once the cooldown has elapsed. Call
        this exactly once per logical delegated-call decision (this is what
        ``delegating()`` does in each ``*_client.py``) — it is not meant to
        be polled speculatively without a follow-up call, since a granted
        HALF_OPEN probe that is never exercised only self-heals after
        another full cooldown window.
        """
        with self._lock:
            now = self._clock()
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                if (
                    self._transitioned_at is not None
                    and (now - self._transitioned_at) >= self.cooldown_seconds
                ):
                    self._state = BreakerState.HALF_OPEN
                    self._transitioned_at = now
                    logger.info(
                        "circuit breaker %s: OPEN -> HALF_OPEN (probe granted)",
                        self.name,
                    )
                    return True
                return False
            # HALF_OPEN — a probe is believed to be in flight.
            if (
                self._transitioned_at is not None
                and (now - self._transitioned_at) >= self.cooldown_seconds
            ):
                # Self-heal: the previous probe never reported an outcome.
                # Grant a fresh probe rather than wedging open forever.
                self._transitioned_at = now
                logger.warning(
                    "circuit breaker %s: stale HALF_OPEN probe, granting a "
                    "new one",
                    self.name,
                )
                return True
            return False

    def record_success(self) -> None:
        """Report a successful delegated call. Closes/resets the breaker."""
        with self._lock:
            if self._state is not BreakerState.CLOSED:
                logger.info(
                    "circuit breaker %s: %s -> CLOSED (call succeeded)",
                    self.name,
                    self._state.value,
                )
            self._state = BreakerState.CLOSED
            self._consecutive_failures = 0
            self._transitioned_at = None

    def record_failure(self) -> None:
        """Report a failed delegated call (transport failure only — see
        module docstring)."""
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.OPEN
                self._transitioned_at = self._clock()
                self._consecutive_failures = max(
                    self._consecutive_failures, self.failure_threshold
                )
                logger.error(
                    "circuit breaker %s: HALF_OPEN probe failed -> OPEN "
                    "(cooldown %.0fs)",
                    self.name,
                    self.cooldown_seconds,
                )
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._transitioned_at = self._clock()
                logger.error(
                    "circuit breaker %s: %d consecutive failures -> OPEN "
                    "(cooldown %.0fs)",
                    self.name,
                    self._consecutive_failures,
                    self.cooldown_seconds,
                )

    def reset(self) -> None:
        """Testing hook — force back to CLOSED with zero failures."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive_failures = 0
            self._transitioned_at = None
