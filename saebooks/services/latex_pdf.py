"""PDF render-service client.

Provides ``render_latex(template, ctx)`` — a thin HTTP client that hands a
document's *facts* (the ``ctx`` dict produced by the engine's ``_build_*_ctx``
builders) to the **app render service** and returns the compiled PDF bytes.

Presentation lives in the app now (#31/#32)
-------------------------------------------
The engine is the *accountant*: it produces facts. The app is the *bookkeeper*:
it owns presentation. The Jinja2 LaTeX templates, the ``latex_escape`` filter,
the letterhead-logo injection, and the ``latex-api`` XeLaTeX client that used to
live in this module have all moved to the app render service
(saebooks-web). This module no longer renders anything itself — it POSTs the
context to the render service and streams back whatever PDF the app produces.

``latex_escape`` knowledge (now owned by the app)
-------------------------------------------------
The old ``latex_escape`` filter escaped the ten LaTeX special characters::

    & % $ # _ { } ~ ^ \\

emitting the tilde, caret and backslash as LaTeX commands
(``\\textasciitilde{}``, ``\\textasciicircum{}``, ``\\textbackslash{}``). It was
order-sensitive: braces ``{`` ``}`` had to be escaped *before* the three
special-command conversions, otherwise the ``{}`` in ``\\textbackslash{}`` would
itself be re-escaped and corrupt the output. That filter (and the templates that
depend on it) now lives in the app render service; the engine no longer escapes
LaTeX because it no longer emits LaTeX. It emits plain JSON facts.

Render-service contract
-----------------------
``render_latex(template, ctx)`` issues::

    POST {RENDER_SERVICE_URL}/internal/render/{template}
    X-Render-Token: {RENDER_SERVICE_TOKEN}   (only when the token is non-empty)
    Content-Type: application/json
    <body> = ctx as JSON

Response handling::

* HTTP 200          → return the response body verbatim (the PDF bytes)
* HTTP 422          → raise ``LatexCompileError`` with the ``log_tail`` field
                      from the JSON body (falling back to ``detail``)
* connection error / timeout / any other status
                    → raise ``LatexServiceError``

Config
------
``RENDER_SERVICE_URL`` (default ``http://web:8080``) and ``RENDER_SERVICE_TOKEN``
(default empty), loaded via ``saebooks.config.settings``.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# Generous ceiling: the app render service does the Jinja render + XeLaTeX
# compile behind this single call, so allow for a slow cold-start compile.
_RENDER_TIMEOUT_SECONDS = 120.0


# ---------------------------------------------------------------------------
# Exceptions (unchanged contract — callers catch these by name)
# ---------------------------------------------------------------------------


class LatexCompileError(Exception):
    """Raised when the render service reports a compile failure (HTTP 422).

    ``log_tail`` is the last few lines of the compile log returned by the
    render service — useful for diagnosing bad template output.
    """

    def __init__(self, log_tail: str) -> None:
        super().__init__(f"LaTeX compile error:\n{log_tail}")
        self.log_tail = log_tail


class LatexServiceError(Exception):
    """Raised for connection errors or unexpected responses from the render service."""


# ---------------------------------------------------------------------------
# Core render function (signature + exception contract preserved)
# ---------------------------------------------------------------------------


async def render_latex(template: str, ctx: dict) -> bytes:
    """POST ``ctx`` to the app render service and return the compiled PDF bytes.

    Parameters
    ----------
    template:
        Template name (e.g. ``"document"``, ``"quote"``, ``"statement_pack"``).
        Selects the app-side template; sent as the final URL path segment.
    ctx:
        The fact context dict. Must be JSON-serialisable (the engine builders
        stringify all Decimals/dates, so it already is).

    Returns
    -------
    bytes
        Raw PDF bytes (begins with ``%PDF``).

    Raises
    ------
    LatexCompileError
        When the render service returns HTTP 422, carrying the compile log tail.
    LatexServiceError
        On connection failure, timeout, or any non-200/422 status.
    """
    from saebooks.config import settings

    base_url = settings.render_service_url.rstrip("/")
    url = f"{base_url}/internal/render/{template}"

    headers: dict[str, str] = {}
    token = settings.render_service_token
    if token:
        headers["X-Render-Token"] = token

    try:
        async with httpx.AsyncClient(timeout=_RENDER_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=ctx, headers=headers)
    except httpx.TimeoutException as exc:
        raise LatexServiceError(
            f"Timeout waiting for render service at {url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        # Covers httpx.ConnectError and every other transport-level failure.
        raise LatexServiceError(
            f"Cannot reach render service at {url}: {exc}"
        ) from exc

    if resp.status_code == 422:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        log_tail = body.get("log_tail") or body.get("detail") or "<no log>"
        raise LatexCompileError(str(log_tail))

    if resp.status_code != 200:
        raise LatexServiceError(
            f"render service {url} returned HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )

    return resp.content
