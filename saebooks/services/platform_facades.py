"""Platform delegation facades — PUBLIC SHIM (delegation off).

The private build mirrors signup / magic-link / webhook requests to the
commercial platform module. In the open engine ``platform_client.delegating()``
is always False, so the call sites never invoke these facades — but the two
public entry points (``mirror_post_json`` / ``mirror_post_raw``) are preserved so
``signup.py`` / ``login.py`` / ``webauthn.py`` / ``principal_auth.py`` import
cleanly. If ever reached, they raise (delegation is off).
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from saebooks.services import platform_client as _client

_DISABLED = (
    "platform delegation is disabled in the open engine — this facade should "
    "not be reached (delegating() is always False)"
)


async def mirror_post_json(
    path: str,
    payload: dict[str, Any],
    *,
    forward_headers: dict[str, str] | None = None,
) -> JSONResponse:  # pragma: no cover - unreachable while delegation off
    raise _client.PlatformServiceError(_DISABLED)


async def mirror_post_raw(
    path: str,
    raw_body: bytes,
    *,
    content_type: str = "application/json",
    forward_headers: dict[str, str] | None = None,
) -> JSONResponse:  # pragma: no cover - unreachable while delegation off
    raise _client.PlatformServiceError(_DISABLED)
