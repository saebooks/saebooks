"""AI document extraction service (B/46).

Accepts a raw file (receipt, supplier invoice, bank statement) and
returns structured accounting data extracted by Claude Haiku via the
LiteLLM proxy (OpenAI-compatible endpoint).

The caller is responsible for checking ``FLAG_AI_EXTRACTION`` before
reaching this module. The endpoint router does that via
``require_feature``.

Usage::

    from saebooks.services.ai_extraction import extract_document

    result = await extract_document(file_bytes, "image/jpeg")
    # result["vendor_name"], result["total"], result["line_items"], …

Returned dict keys
------------------
vendor_name         str | None
vendor_abn          str | None   Australian Business Number, 11 digits (issue #33 phase 2)
invoice_number      str | None
date                str | None   ISO-8601 date string (YYYY-MM-DD)
due_date            str | None   ISO-8601 date string (YYYY-MM-DD)
subtotal            str | None   decimal string, e.g. "123.45"
tax_amount          str | None   decimal string
total               str | None   decimal string
currency            str | None   ISO-4217 code, e.g. "AUD"
line_items          list[dict]   each: {description, qty, unit_price, amount, tax_code}
notes               str | None
extraction_error    str | None   set only when the API call failed / partial result
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from saebooks.config import Settings
from saebooks.config import settings as _default_settings

logger = logging.getLogger("saebooks.ai_extraction")

# ---------------------------------------------------------------------- #
# Errors                                                                  #
# ---------------------------------------------------------------------- #


class AiExtractionError(RuntimeError):
    """Base class for all AI extraction errors."""


class AiExtractionNotConfiguredError(AiExtractionError):
    """Raised when LITELLM_API_KEY is not set."""


# ---------------------------------------------------------------------- #
# Constants                                                               #
# ---------------------------------------------------------------------- #

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """You are an accounting document parser. Extract structured data from
the supplied document (receipt, invoice, bank statement, or similar) and return ONLY a
valid JSON object with the following keys:

  vendor_name       - string or null
  vendor_abn        - string of exactly 11 digits or null (Australian Business
                      Number, often labelled "ABN"; strip spaces, digits only)
  invoice_number    - string or null
  date              - ISO-8601 date string (YYYY-MM-DD) or null
  due_date          - ISO-8601 date string (YYYY-MM-DD) or null
  subtotal          - decimal string (e.g. "123.45") or null
  tax_amount        - decimal string or null
  total             - decimal string or null
  currency          - ISO-4217 three-letter code (e.g. "AUD") or null
  line_items        - array of objects, each with keys:
                        description  (string or null)
                        qty          (decimal string or null)
                        unit_price   (decimal string or null)
                        amount       (decimal string or null)
                        tax_code     (string or null, e.g. "GST", "GST-FREE")
  notes             - any free-text notes, payment terms, or footer text (string or null)

Rules:
- Return ONLY the JSON object, no markdown fencing, no explanation.
- Use null for any field you cannot confidently determine.
- All numeric values must be strings, not numbers.
- Dates must be YYYY-MM-DD strings or null.
- currency should be the document's stated currency; guess "AUD" for Australian documents
  that omit the currency symbol but show $ amounts, only if confident.
- line_items may be an empty array if no line detail is present.
"""

# ---------------------------------------------------------------------- #
# Public API                                                              #
# ---------------------------------------------------------------------- #


async def extract_document(
    file_bytes: bytes,
    mime_type: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Extract structured accounting data from a document image or PDF.

    Parameters
    ----------
    file_bytes:
        Raw bytes of the uploaded file.
    mime_type:
        MIME type string — one of ``image/jpeg``, ``image/png``,
        ``image/webp``, or ``application/pdf``.
    settings:
        Optional ``Settings`` override for testing; falls back to the
        module-level singleton.

    Returns
    -------
    dict
        Structured extraction result. ``extraction_error`` key is present
        and non-None only when the LiteLLM call failed; in that case
        all other fields default to ``None`` and ``line_items`` to ``[]``.
    """
    # Capture-module delegation (#32 step 5). When ``CAPTURE_BASE_URL`` is set
    # AND no explicit ``settings`` override was passed (test overrides always
    # run in-process against the injected LLM config), post the file to the
    # capture module and return its dict unchanged. The module runs this same
    # function with the flag OFF, so there is no recursion.
    if settings is None:
        from saebooks.services import capture_client as _capture

        if _capture.delegating():
            from saebooks.services import capture_facades as _cf

            return await _cf.extract_document(file_bytes, mime_type)

    effective = settings if settings is not None else _default_settings

    if not effective.litellm_api_key:
        raise AiExtractionNotConfiguredError(
            "LITELLM_API_KEY is not configured; cannot reach the LiteLLM proxy."
        )

    b64_data = base64.standard_b64encode(file_bytes).decode()
    data_url = f"data:{mime_type};base64,{b64_data}"

    # Anthropic (via the LiteLLM proxy) accepts only image/jpeg|png|gif|webp
    # inside an ``image_url`` block; a PDF must travel as a ``file`` content
    # block, which LiteLLM translates to Anthropic's document block.
    if mime_type == "application/pdf":
        document_part: dict[str, Any] = {
            "type": "file",
            "file": {"filename": "document.pdf", "file_data": data_url},
        }
    else:
        document_part = {"type": "image_url", "image_url": {"url": data_url}}

    payload: dict[str, Any] = {
        "model": _MODEL,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    document_part,
                    {
                        "type": "text",
                        "text": (
                            "Extract the accounting data from this document "
                            "and return it as JSON per the system instructions."
                        ),
                    },
                ],
            },
        ],
    }

    url = effective.litellm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {effective.litellm_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("LiteLLM API error during document extraction: %s", exc)
        return _error_result(str(exc))

    try:
        raw_text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Unexpected LiteLLM response shape: %s — data: %.200s", exc, data)
        return _error_result(f"Unexpected response shape: {exc}")

    return _parse_response(raw_text)


# ---------------------------------------------------------------------- #
# Internal helpers                                                        #
# ---------------------------------------------------------------------- #


def _empty_result() -> dict[str, Any]:
    """Return a fully-structured empty result (all fields None / empty list)."""
    return {
        "vendor_name": None,
        "vendor_abn": None,
        "invoice_number": None,
        "date": None,
        "due_date": None,
        "subtotal": None,
        "tax_amount": None,
        "total": None,
        "currency": None,
        "line_items": [],
        "notes": None,
        "extraction_error": None,
    }


def _error_result(error_msg: str) -> dict[str, Any]:
    result = _empty_result()
    result["extraction_error"] = error_msg
    return result


def _parse_response(raw_text: str) -> dict[str, Any]:
    """Parse the model's JSON response into the canonical dict shape.

    If parsing fails or keys are missing we return what we can plus an
    ``extraction_error`` note so the caller can surface a graceful UI.
    """
    text = raw_text.strip()

    # Strip optional markdown code fences the model might add despite the
    # system prompt saying not to.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence line and closing fence if present.
        inner = lines[1:]
        if inner and inner[-1].strip().startswith("```"):
            inner = inner[:-1]
        text = "\n".join(inner)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse model response as JSON: %s — raw: %.200s", exc, raw_text)
        result = _error_result(f"JSON parse error: {exc}")
        return result

    if not isinstance(parsed, dict):
        return _error_result("Model returned non-object JSON")

    result = _empty_result()

    for scalar_key in (
        "vendor_name",
        "vendor_abn",
        "invoice_number",
        "date",
        "due_date",
        "subtotal",
        "tax_amount",
        "total",
        "currency",
        "notes",
    ):
        value = parsed.get(scalar_key)
        result[scalar_key] = str(value) if value is not None else None

    raw_lines = parsed.get("line_items")
    if isinstance(raw_lines, list):
        result["line_items"] = [
            {
                "description": item.get("description"),
                "qty": item.get("qty"),
                "unit_price": item.get("unit_price"),
                "amount": item.get("amount"),
                "tax_code": item.get("tax_code"),
            }
            for item in raw_lines
            if isinstance(item, dict)
        ]

    return result
