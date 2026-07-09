"""API-token scope enforcement (A2 privilege-escalation fix).

``ApiToken.scopes`` used to be stored, echoed, and shown in the admin
UI but never read for an authorization decision — so a token a
consumer believed was "read-only" could still POST/PUT/PATCH/DELETE
(a deferred "per-scope authorization" item from the original build
brief: *every token currently has the user's full role-level access*).
This module is the decision layer
``require_bearer`` now consults for ``saebk_*`` API-token auth ONLY.

Design — minimum viable (read vs write)
---------------------------------------
The brief calls for the minimum viable matrix, not a per-domain one:

    GET / HEAD / OPTIONS   -> require ``read``
    POST / PUT / PATCH / DELETE / <anything else> -> require ``write``

A ``write``-scoped token may also read: mutations routinely read then
write, and a write-capable consumer reading is not an escalation.

Backward compatibility (critical — must not break the live dogfood)
-------------------------------------------------------------------
``services.api_tokens.issue`` defaults ``scopes=[]`` and nothing in
the issuance path ever set a restrictive scope, so EVERY token in the
wild today has empty scopes. Those must keep FULL access. A token is
treated as full-access (unchanged behaviour) when its scopes are:

* empty or ``None`` (the default / every existing token), OR
* contain the wildcard marker ``"*"``, OR
* contain the literal ``"full"``, OR
* contain BOTH ``"read"`` and ``"write"`` (the two verbs together ==
  unrestricted in this read/write model).

Only an explicitly restrictive set — e.g. ``["read"]`` — is limited.
Comparison is case-insensitive and whitespace-trimmed so a UI that
stores ``"Read"`` or ``" write "`` still behaves.
"""
from __future__ import annotations

from collections.abc import Iterable

SCOPE_READ = "read"
SCOPE_WRITE = "write"

# Explicit full-access markers (any one present => unrestricted).
FULL_ACCESS_MARKERS = frozenset({"*", "full"})

# HTTP methods that only read state. Everything else is treated as a
# mutation and requires the write scope (fail-closed for unknown verbs).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalise(scopes: Iterable[str] | None) -> set[str]:
    """Lower-case + strip the scope tokens; drop blanks."""
    if not scopes:
        return set()
    out: set[str] = set()
    for s in scopes:
        if s is None:
            continue
        v = str(s).strip().lower()
        if v:
            out.add(v)
    return out


def method_requires_scope(method: str) -> str:
    """Return the scope a given HTTP method requires.

    Safe methods (GET/HEAD/OPTIONS) require ``read``; everything else
    (POST/PUT/PATCH/DELETE and any unknown verb — fail-closed) requires
    ``write``.
    """
    return SCOPE_READ if method.upper() in _SAFE_METHODS else SCOPE_WRITE


def is_full_access(scopes: Iterable[str] | None) -> bool:
    """True iff this scope set grants unrestricted (legacy) access.

    Backward-compat guarantee: empty/None or a full marker or both
    verbs => full access, exactly as before scope enforcement existed.
    """
    norm = _normalise(scopes)
    if not norm:
        return True
    if norm & FULL_ACCESS_MARKERS:
        return True
    return bool(SCOPE_READ in norm and SCOPE_WRITE in norm)


def token_allows(scopes: Iterable[str] | None, method: str) -> bool:
    """Authorize an HTTP ``method`` against a token's ``scopes``.

    * Full-access tokens (see ``is_full_access``) allow everything —
      this preserves every existing token's behaviour.
    * Otherwise the token must carry the scope the method requires.
      A ``write`` scope additionally satisfies a ``read`` requirement
      (write implies read); a ``read`` scope does NOT satisfy ``write``.
    """
    if is_full_access(scopes):
        return True
    norm = _normalise(scopes)
    required = method_requires_scope(method)
    if required == SCOPE_READ:
        # read OR write satisfies a read requirement.
        return bool(norm & {SCOPE_READ, SCOPE_WRITE})
    # required == write
    return SCOPE_WRITE in norm


__all__ = [
    "FULL_ACCESS_MARKERS",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "is_full_access",
    "method_requires_scope",
    "token_allows",
]
