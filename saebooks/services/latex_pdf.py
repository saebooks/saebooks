"""LaTeX PDF rendering engine.

Provides ``render_latex(template, ctx)`` — a general-purpose function that
renders a Jinja2 LaTeX template, POSTs the resulting source to the
``latex-api`` microservice, and returns the compiled PDF bytes.

Template conventions
--------------------
Templates live in ``saebooks/templates/latex/<name>.tex.j2``.

Jinja2 delimiters are kept at the standard ``{{ }}`` / ``{% %}`` / ``{# #}``
because LaTeX documents do not normally use those sequences.  The one overlap
is ``{% %}`` which clashes with LaTeX's ``\begin{%...}`` — but that construct
is vanishingly rare.  If a future template needs it, use ``{% raw %}...{% endraw %}``.

Every value interpolated into LaTeX source MUST be run through the
``latex_escape`` Jinja2 filter to prevent special-character injection that
would break compilation or change document meaning.  The filter escapes the
ten LaTeX special characters::

    & % $ # _ { } ~ ^ \\

The tilde, caret, and backslash are emitted as LaTeX commands::

    ~ → \\textasciitilde{}
    ^ → \\textasciicircum{}
    \\ → \\textbackslash{}

Implementation note: braces ``{`` ``}`` are escaped first, then the three
special-command conversions (tilde, caret, backslash) are applied.  If the
order were reversed, the ``{}`` in ``\\textbackslash{}`` would itself be
escaped to ``\\{\\}`` — corrupting the output.  The full order is::

    { } & % $ # _  →  then  ~ ^ \\

Error handling
--------------
* HTTP 422 from latex-api → ``LatexCompileError(log_tail)``
* Connection / timeout error → ``LatexServiceError``
* HTTP 2xx but no ``pdf_url`` → ``LatexServiceError``

Config
------
``LATEX_API_URL`` env var (default ``http://latex-api:8000``), loaded via
``saebooks.config.settings``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx
import jinja2

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LatexCompileError(Exception):
    """Raised when latex-api returns HTTP 422 (xelatex failed).

    ``log_tail`` is the last few lines of the xelatex log returned in the
    422 ``detail`` field — useful for diagnosing bad template output.
    """

    def __init__(self, log_tail: str) -> None:
        super().__init__(f"LaTeX compile error:\n{log_tail}")
        self.log_tail = log_tail


class LatexServiceError(Exception):
    """Raised for connection errors or unexpected responses from latex-api."""


# ---------------------------------------------------------------------------
# latex_escape filter
# ---------------------------------------------------------------------------

# Single-pass regex for LaTeX special-character escaping.
# Using sequential str.replace() calls is dangerous because replacements
# introduced in earlier passes (e.g. \& from &) would be re-processed by
# the backslash pass.  A single re.sub call with a function replacement
# processes each character exactly once.
_LATEX_ESCAPE_RE = re.compile(r'([&%$#_{}~^\\])')

_LATEX_CHAR_MAP: dict[str, str] = {
    "\\":  r"\textbackslash{}",
    "{":   r"\{",
    "}":   r"\}",
    "&":   r"\&",
    "%":   r"\%",
    "$":   r"\$",
    "#":   r"\#",
    "_":   r"\_",
    "~":   r"\textasciitilde{}",
    "^":   r"\textasciicircum{}",
}


def latex_escape(value: Any) -> str:
    """Escape a Python value for safe interpolation into LaTeX source.

    Uses a single-pass regex substitution so each source character is
    replaced exactly once; backslashes introduced by earlier replacements
    are never re-processed.

    Usage in templates::

        {{ company.legal_name | latex_escape }}
        {{ amount | latex_escape }}
    """
    text = str(value) if not isinstance(value, str) else value
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_CHAR_MAP[m.group(1)], text)


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "latex"

_env: jinja2.Environment | None = None


def _get_env() -> jinja2.Environment:
    global _env
    if _env is None:
        _env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=False,  # LaTeX, not HTML — escaping is manual via latex_escape
            keep_trailing_newline=True,
        )
        _env.filters["latex_escape"] = latex_escape
    return _env


# ---------------------------------------------------------------------------
# Core render function
# ---------------------------------------------------------------------------


async def render_latex(template: str, ctx: dict) -> bytes:
    """Render ``<template>.tex.j2`` with ``ctx`` and return PDF bytes.

    Parameters
    ----------
    template:
        Template name without the ``.tex.j2`` suffix (e.g. ``"statement_pack"``).
    ctx:
        Context dict passed to Jinja2.  Values interpolated in the template
        MUST use the ``latex_escape`` filter.

    Returns
    -------
    bytes
        Raw PDF bytes (begins with ``%PDF``).

    Raises
    ------
    LatexCompileError
        When latex-api returns HTTP 422, including the xelatex log tail.
    LatexServiceError
        On connection failure, unexpected HTTP status, or missing ``pdf_url``
        in the compile response.
    jinja2.TemplateNotFound
        When the named template does not exist in the template directory.
    """
    from saebooks.config import settings

    latex_api_url = settings.latex_api_url

    # Render the Jinja2 template to a LaTeX source string.
    env = _get_env()
    tmpl = env.get_template(f"{template}.tex.j2")
    latex_src = tmpl.render(**ctx)

    # POST the LaTeX source to latex-api /compile.
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            compile_resp = await client.post(
                f"{latex_api_url}/compile",
                json={"latex": latex_src},
            )
    except httpx.ConnectError as exc:
        raise LatexServiceError(
            f"Cannot connect to latex-api at {latex_api_url}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise LatexServiceError(
            f"Timeout waiting for latex-api at {latex_api_url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise LatexServiceError(
            f"HTTP error communicating with latex-api: {exc}"
        ) from exc

    if compile_resp.status_code == 422:
        detail = compile_resp.json().get("detail", "<no log>")
        raise LatexCompileError(str(detail))

    if compile_resp.status_code != 200:
        raise LatexServiceError(
            f"latex-api /compile returned HTTP {compile_resp.status_code}: "
            f"{compile_resp.text[:500]}"
        )

    compile_body = compile_resp.json()
    pdf_url = compile_body.get("pdf_url")
    if not pdf_url:
        raise LatexServiceError(
            f"latex-api /compile response missing 'pdf_url': {compile_body}"
        )

    # GET the compiled PDF bytes.
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            pdf_resp = await client.get(f"{latex_api_url}{pdf_url}")
    except httpx.RequestError as exc:
        raise LatexServiceError(
            f"HTTP error fetching PDF from latex-api: {exc}"
        ) from exc

    if pdf_resp.status_code != 200:
        raise LatexServiceError(
            f"latex-api GET {pdf_url} returned HTTP {pdf_resp.status_code}"
        )

    return pdf_resp.content
