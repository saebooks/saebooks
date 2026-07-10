"""Platform-module client — PUBLIC SHIM (delegation off in the open engine).

The private build can delegate identity ceremonies (signup / login / webauthn /
principal-login) and the Stripe webhook to a sibling ``platform-web`` container
(the commercial SaaS control plane). The open engine runs all of that
in-process: ``delegating()`` is always False, so every call site
(``signup.py`` / ``login.py`` / ``webauthn.py`` / ``principal_auth.py`` — each
shaped ``if delegating(): <delegate> else: <in-process>``) takes its in-process
branch, where the same process both mints and verifies its own tokens.

Public symbols preserved: ``delegating`` (→ False), ``verify_key_parity_or_disable``
(→ False, no-op preflight), ``disable_delegation`` / the test-reset hooks, and
the transport helpers ``post_json`` / ``post_raw`` / ``json_body`` /
``PlatformServiceError`` (never reached while delegation is off, but kept so the
facade module imports cleanly).
"""
from __future__ import annotations

from typing import Any

from saebooks.services.circuit_breaker import DelegatedServiceError

_DISABLED = (
    "platform delegation is disabled in the open engine — identity ceremonies "
    "run in-process"
)


class PlatformServiceError(DelegatedServiceError):
    """Preserved for callers that catch it; never raised while delegation off."""

    module = "platform"


def delegating() -> bool:
    """Always False: the open engine runs identity in-process."""
    return False


def disable_delegation() -> None:
    """No-op — delegation is already off in the open engine."""


async def verify_key_parity_or_disable() -> bool:
    """Boot preflight no-op — nothing to delegate, so nothing to verify."""
    return False


def jsonable(value: Any) -> Any:
    """Preserved helper (unused while delegation is off)."""
    return value


def json_body(resp: Any, url_hint: str = "") -> Any:  # pragma: no cover - unreachable
    raise PlatformServiceError(_DISABLED)


async def post_json(path: str, payload: dict[str, Any], **_: Any) -> Any:  # pragma: no cover
    raise PlatformServiceError(_DISABLED)


async def post_raw(path: str, content: bytes, **_: Any) -> Any:  # pragma: no cover
    raise PlatformServiceError(_DISABLED)


def _reset_delegation_for_tests() -> None:
    """Test hook — delegation is always off; nothing to reset."""


def _reset_breaker_for_tests() -> None:
    """Test hook — no runtime breaker in the open engine."""
