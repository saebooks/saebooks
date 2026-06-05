"""Tests for saebooks.services.latex_pdf.

Tests:
* test_latex_escape_* — unit tests of latex_escape filter
* test_render_latex_success — respx mock: compile POST → pdf_url, GET → bytes
* test_render_latex_compile_error — respx mock: compile POST → 422
* test_render_latex_connection_error — respx mock: connection error
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from saebooks.services.latex_pdf import (
    LatexCompileError,
    LatexServiceError,
    latex_escape,
)

# ---------------------------------------------------------------------------
# latex_escape unit tests
# ---------------------------------------------------------------------------


def test_latex_escape_ampersand() -> None:
    assert latex_escape("Smith & Sons") == r"Smith \& Sons"


def test_latex_escape_dollar() -> None:
    assert latex_escape("$100.00") == r"\$100.00"


def test_latex_escape_percent() -> None:
    assert latex_escape("15%") == r"15\%"


def test_latex_escape_hash() -> None:
    assert latex_escape("#1 supplier") == r"\#1 supplier"


def test_latex_escape_underscore() -> None:
    assert latex_escape("account_code") == r"account\_code"


def test_latex_escape_braces() -> None:
    assert latex_escape("{value}") == r"\{value\}"


def test_latex_escape_tilde() -> None:
    assert latex_escape("~") == r"\textasciitilde{}"


def test_latex_escape_caret() -> None:
    assert latex_escape("^") == r"\textasciicircum{}"


def test_latex_escape_backslash() -> None:
    """Backslash escapes to \\textbackslash{} — braces are NOT re-escaped."""
    result = latex_escape("\\")
    assert result == r"\textbackslash{}"


def test_latex_escape_combined() -> None:
    """Company name with & and amount with $ % _ all escape correctly."""
    raw = "Smith & Jones Pty Ltd $1,000_balance (50%)"
    result = latex_escape(raw)
    assert r"\&" in result
    assert r"\$" in result
    assert r"\%" in result
    assert r"\_" in result
    # Ensure literal specials are not present (except escaped forms)
    assert "&" not in result.replace(r"\&", "")
    assert "$" not in result.replace(r"\$", "")
    assert "%" not in result.replace(r"\%", "")
    assert "_" not in result.replace(r"\_", "")


def test_latex_escape_non_string_coercion() -> None:
    """Non-string values are coerced to str before escaping."""
    assert latex_escape(12345) == "12345"
    assert latex_escape(None) == "None"


def test_latex_escape_no_mutation() -> None:
    """Backslash converts to \\textbackslash{} whose braces are NOT re-escaped.

    The input is a raw backslash followed by &.  Expected result:
      \\textbackslash{}\\&
    NOT \\textbackslash\\{\\}\\& (which would mean the {} got re-escaped).
    """
    raw = "\\&"
    result = latex_escape(raw)
    # & is escaped first (braces, &, %, $ etc.) in steps 1+2, but \\ last.
    # Actually: step-1 escapes braces (none here), step-2 escapes &→\&,
    # step-3 converts \→\textbackslash{}.
    # So "\\&" → "\\\\&" after step-2? No — step-2 only touches & % $ # _.
    # After step-2: "\\&" becomes "\\\&" (& escaped).
    # After step-3 (backslash): "\\\&" → "\textbackslash{}\&"
    assert r"\textbackslash{}" in result
    assert r"\&" in result
    # The {} in \textbackslash{} must be literal, not escaped braces
    assert r"\textbackslash\{" not in result


# ---------------------------------------------------------------------------
# render_latex — respx mocking
# ---------------------------------------------------------------------------

_FAKE_PDF = b"%PDF-1.5 fake pdf content"
_FAKE_PDF_URL = "/files/abc123.pdf"
_FAKE_COMPILE_RESPONSE = {"status": "ok", "pdf_url": _FAKE_PDF_URL, "id": "abc123"}
_LATEX_API_BASE = "http://latex-api:8000"


@pytest.fixture(autouse=True)
def _restore_latex_env():
    """Snapshot and restore the module-global Jinja2 ``_env``.

    ``_setup_test_env`` swaps ``saebooks.services.latex_pdf._env`` for a
    DictLoader-backed environment.  Without restoration that DictLoader
    leaks into the rest of the test session, so any later test that calls
    ``render_latex("document", ...)`` against the real FileSystemLoader
    instead hits the leaked DictLoader and raises ``TemplateNotFound``.
    This autouse fixture resets the global after every test in this
    module, regardless of pass/fail.
    """
    import saebooks.services.latex_pdf as _svc

    saved = _svc._env
    try:
        yield
    finally:
        _svc._env = saved


def _setup_test_env(extra_templates: dict[str, str] | None = None) -> None:
    """Replace the global Jinja2 env loader with a DictLoader for testing.

    ``extra_templates`` maps template names (without .tex.j2) to source strings.
    The dict is merged with a minimal _preamble.tex.j2 so include directives work.
    """
    import jinja2

    import saebooks.services.latex_pdf as _svc

    _svc._env = None
    env = _svc._get_env()

    templates = {
        "_preamble.tex.j2": (
            r"\documentclass[11pt,a4paper]{article}"
            "\n"
            r"\usepackage{geometry}"
            "\n"
            r"\usepackage{fontspec}"
            "\n"
            r"\setmainfont{Liberation Sans}"
            "\n"
        ),
    }
    if extra_templates:
        templates.update(extra_templates)

    env.loader = jinja2.DictLoader(templates)


@pytest.mark.asyncio
async def test_render_latex_success(respx_mock: respx.MockRouter) -> None:
    """Happy path: compile → pdf_url → GET pdf bytes; escaped values present."""
    import os

    import saebooks.services.latex_pdf as _svc

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE
    _setup_test_env({
        "test_tmpl.tex.j2": (
            r"\documentclass{article}"
            "\n"
            r"\begin{document}"
            "\n"
            "{{ name | latex_escape }}"
            "\n"
            r"\end{document}"
        ),
    })

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(200, json=_FAKE_COMPILE_RESPONSE)
    )
    respx_mock.get(f"{_LATEX_API_BASE}{_FAKE_PDF_URL}").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )

    pdf = await _svc.render_latex("test_tmpl", {"name": "Sauer & Sons"})
    assert pdf == _FAKE_PDF

    # Verify the POST included the escaped LaTeX.
    compile_call = respx_mock.calls[0]
    posted_body = compile_call.request.content.decode()
    assert "Sauer" in posted_body
    assert r"\&" in posted_body  # & was escaped


@pytest.mark.asyncio
async def test_render_latex_compile_error(respx_mock: respx.MockRouter) -> None:
    """422 from /compile raises LatexCompileError with the log tail."""
    import os

    import saebooks.services.latex_pdf as _svc

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE
    _setup_test_env({
        "bad_tmpl.tex.j2": (
            r"\documentclass{article}\begin{document}bad\end{document}"
        ),
    })

    log_tail = "! Undefined control sequence.\nl.5 \\badcommand"
    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        return_value=Response(422, json={"detail": log_tail})
    )

    with pytest.raises(LatexCompileError) as exc_info:
        await _svc.render_latex("bad_tmpl", {})

    assert log_tail in exc_info.value.log_tail


@pytest.mark.asyncio
async def test_render_latex_connection_error(respx_mock: respx.MockRouter) -> None:
    """Connection failure raises LatexServiceError (not a raw httpx error)."""
    import os

    import httpx

    import saebooks.services.latex_pdf as _svc

    os.environ["LATEX_API_URL"] = _LATEX_API_BASE
    _setup_test_env({
        "conn_tmpl.tex.j2": (
            r"\documentclass{article}\begin{document}x\end{document}"
        ),
    })

    respx_mock.post(f"{_LATEX_API_BASE}/compile").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(LatexServiceError):
        await _svc.render_latex("conn_tmpl", {})
