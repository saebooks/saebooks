"""Per-request developer-mode context.

Carries opt-in dev overrides set by the SkipAuditMiddleware (and any future
header-driven dev toggles). Read by service-layer guards
(e.g. ``change_log_svc.append``) that decide whether to short-circuit.

The contextvar is set inside the middleware coroutine before the handler
runs and reset on the way out — survives the entire async stack including
nested ``session.flush`` / ``session.commit`` calls.
"""
from __future__ import annotations

from contextvars import ContextVar

# True when the current request is opting out of audit-trail writes.
# Set ONLY by SkipAuditMiddleware after verifying:
#   * X-Dev-Skip-Audit: true on the request
#   * caller is admin (request.state.user.role)
#   * SAEBOOKS_EDITION puts FLAG_SKIP_AUDIT_TRAIL in active flags
_skip_audit: ContextVar[bool] = ContextVar("saebooks_skip_audit", default=False)


def skip_audit_active() -> bool:
    """True when the current request is in audit-skip mode."""
    return _skip_audit.get()


def set_skip_audit(value: bool):
    """Set the contextvar. Returns the token so the caller can reset."""
    return _skip_audit.set(value)


def reset_skip_audit(token):
    """Reset using a token returned from ``set_skip_audit``."""
    _skip_audit.reset(token)
