"""extract.py — OCR text → ExtractedStatement via LLM.

Ported from the statement-recon-prototype's extract.py. The LLM call is
isolated behind a module-level ``_call_llm`` function so tests can
monkeypatch it without spinning up a real litellm gateway.

Configuration keys added to saebooks/config.py:
  statement_llm_base   (STATEMENT_LLM_BASE)
  statement_llm_model  (STATEMENT_LLM_MODEL)
  statement_llm_model_escalation (STATEMENT_LLM_MODEL_ESCALATION)
  statement_llm_api_key (STATEMENT_LLM_API_KEY)
  statement_llm_vision_model (STATEMENT_LLM_VISION_MODEL)
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from saebooks.config import Settings

logger = logging.getLogger("saebooks.statements.extract")

# ---------------------------------------------------------------------------
# Type codes → canonical line type strings (model enum .values)
# ---------------------------------------------------------------------------
_TYPE_CODE_MAP: dict[str, str] = {
    "IN": "invoice",
    "INV": "invoice",
    "INVOICE": "invoice",
    "PY": "payment",
    "PAY": "payment",
    "PAYMENT": "payment",
    "CR": "credit",
    "CREDIT": "credit",
    "CN": "credit",
    "ADJ": "adjustment",
    "ADJUSTMENT": "adjustment",
}

# ---------------------------------------------------------------------------
# Plain dataclasses (no ORM dependency — used as transport between pipeline
# stages; the ingest layer maps these onto ORM rows).
# ---------------------------------------------------------------------------

@dataclass
class ExtractedLine:
    line_date: date | None
    line_type: str                   # one of StatementLineType enum values
    reference: str | None
    description: str | None
    amount: Decimal                  # signed: invoice +, payment/credit −


@dataclass
class ExtractedStatement:
    supplier_name: str | None
    supplier_abn: str | None
    customer_ref: str | None
    statement_date: date | None
    terms: str | None
    closing_balance: Decimal | None
    opening_balance: Decimal | None
    lines: list[ExtractedLine] = field(default_factory=list)
    model_used: str = ""
    escalated: bool = False


# ---------------------------------------------------------------------------
# LLM system prompt (ported verbatim from prototype, with model param removed)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are a data-extraction assistant. You receive OCR text from an Australian supplier
statement and must return STRICT JSON — no markdown, no commentary, no code fences.

Return a single JSON object with these fields:
{
  "supplier_name": string or null,
  "supplier_abn": string or null,        // e.g. "32 000 143 608"
  "customer_ref": string or null,        // the supplier's account number FOR the buyer
  "statement_date": string or null,      // ISO 8601 YYYY-MM-DD
  "terms": string or null,               // e.g. "30 Days"
  "closing_balance": number or null,     // total amount owed per the statement
  "opening_balance": number or null,
  "lines": [
    {
      "date": string or null,            // YYYY-MM-DD
      "type": string,                    // use the raw type code: IN, PY, CR, ADJ, etc.
      "reference": string or null,       // supplier's invoice/doc number
      "description": string or null,     // customer PO reference or free-text description (NOT the amount)
      "amount": number                   // signed: positive for invoices, negative for payments/credits
    }
  ]
}

Rules:
- supplier_name: the company issuing this statement (the entity whose ABN is shown). It is NOT
  the buyer/recipient. Look for it near "ABN:" in the header block — it is typically the company
  name directly above or beside the ABN line. Examples: "Motion Australia Pty Ltd", "Bearing
  Supplies Co", etc.
- supplier_abn: the ABN shown near the supplier's company name (e.g. "32 000 143 608").
- customer_ref: the buyer's account number at the supplier — look for "Customer:" field.
- statement_date: look for "Date:" or "Statement Date:" in the header.
- Each line in the statement body (invoice, payment, credit, adjustment) becomes one entry in "lines".
- Payments (PY) and credits (CR) are NEGATIVE amounts.
- Invoices (IN) are POSITIVE amounts.
- The closing_balance is the total currently owed — look for "$DUE" or "Balance Due" or the final
  balance figure. Do NOT use per-line running balances.
- If closing_balance is not explicitly stated, derive it as the sum of all line amounts.
- LINE AMOUNTS: use each line's INVOICE TOTAL (including GST), never a GST/tax-only
  column. If the statement shows separate columns for a tax/GST component and an
  invoice total (or a running balance), use the invoice total figure for that line —
  do NOT use the tax-component column. The sum of open invoice line amounts should
  reconcile to the closing_balance; if it doesn't, you have picked the wrong column.
- statement_date fallback: also accept "Reference Date:", "Month Ended", "as at <date>",
  or a period-ending date in the header. If no header date is labelled at all, use the
  latest (max) line date.
- Do not duplicate lines. Each actual transaction appears once.
- Amounts use dot as decimal separator (Australian dollars).
- Return ONLY the JSON object. No markdown. No explanation.
"""


# ---------------------------------------------------------------------------
# Injectable LLM call — monkeypatch this in tests
# ---------------------------------------------------------------------------

async def _call_llm(
    prompt_system: str,
    prompt_user: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
) -> str:
    """POST to an OpenAI-compatible /chat/completions endpoint.

    Returns the assistant message content string.
    Retries up to 3 times with linear back-off on transient errors.
    Raises RuntimeError on exhausted retries.
    """
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=90.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                return body["choices"][0]["message"]["content"]
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "litellm attempt %d failed: %s; retrying", attempt + 1, exc
                )
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"LLM call failed after 3 attempts: {last_err}")


async def _call_llm_vision(
    prompt_system: str,
    image_bytes: bytes,
    mime_type: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
) -> str:
    """POST a vision message to an OpenAI-compatible /chat/completions endpoint.

    Encodes ``image_bytes`` as base64 and sends a multimodal user message.
    Note: PDF documents (mime application/pdf) are not natively supported as
    image_url by most vision models — the call is still attempted and any
    failure raises RuntimeError, which the ingest layer catches to persist a
    NEEDS_REVIEW row (see ingest.ingest_statement step 4) rather than 5xx-ing.

    Returns the assistant message content string.
    Raises RuntimeError on exhausted retries.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt_system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract this supplier statement of account as STRICT JSON per the schema.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                    },
                ],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url.rstrip("/") + "/chat/completions"

    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                return body["choices"][0]["message"]["content"]
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "litellm vision attempt %d failed: %s; retrying", attempt + 1, exc
                )
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"LLM vision call failed after 3 attempts: {last_err}")


# ---------------------------------------------------------------------------
# Parsing helpers (ported from prototype)
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    logger.warning("could not parse date %r", s)
    return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    s = re.sub(r"[A-Z]{3}$", "", s).strip()
    if not s or s in ("-", ""):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        logger.warning("could not parse decimal %r", value)
        return None


def _classify_type(raw_type: Any) -> str:
    """Map raw LLM type code to a StatementLineType enum value string."""
    if not raw_type:
        return "unknown"
    key = str(raw_type).strip().upper()
    return _TYPE_CODE_MAP.get(key, "unknown")


def _map_line(raw: dict) -> ExtractedLine | None:
    try:
        amount = _parse_decimal(raw.get("amount"))
        if amount is None:
            logger.warning("skipping line with unparseable amount: %s", raw)
            return None
        line_type = _classify_type(raw.get("type"))
        # Payments and credits must be negative
        if line_type in ("payment", "credit") and amount > 0:
            amount = -amount
        return ExtractedLine(
            line_date=_parse_date(raw.get("date")),
            line_type=line_type,
            reference=raw.get("reference") or None,
            description=raw.get("description") or None,
            amount=amount,
        )
    except Exception as exc:
        logger.warning("error mapping line %s: %s", raw, exc)
        return None


def _parse_response(raw_response: str) -> dict:
    """Strip fences and parse JSON from LLM response."""
    clean = _strip_fences(raw_response)
    return json.loads(clean)


def _build_extracted_statement(data: dict, model_used: str, escalated: bool) -> ExtractedStatement:
    """Convert parsed LLM JSON dict into an ExtractedStatement dataclass."""
    lines: list[ExtractedLine] = []
    for raw_line in data.get("lines", []):
        mapped = _map_line(raw_line)
        if mapped is not None:
            lines.append(mapped)

    closing_balance = _parse_decimal(data.get("closing_balance"))
    opening_balance = _parse_decimal(data.get("opening_balance"))

    # Derive closing_balance from lines if not stated
    if closing_balance is None and lines:
        derived = sum(ln.amount for ln in lines)
        if derived < 0:
            derived = abs(derived)
        closing_balance = derived
        logger.info("closing_balance derived from lines: %s", closing_balance)

    return ExtractedStatement(
        supplier_name=data.get("supplier_name") or None,
        supplier_abn=data.get("supplier_abn") or None,
        customer_ref=str(data["customer_ref"]) if data.get("customer_ref") is not None else None,
        statement_date=_parse_date(data.get("statement_date")),
        terms=data.get("terms") or None,
        closing_balance=closing_balance,
        opening_balance=opening_balance,
        lines=lines,
        model_used=model_used,
        escalated=escalated,
    )


def _build_system_prompt(prompt_hint: str | None) -> str:
    """Return the system prompt, optionally appending a supplier-specific hint."""
    if not prompt_hint:
        return _SYSTEM_PROMPT
    return (
        _SYSTEM_PROMPT
        + "\n\nSupplier-specific extraction guidance"
        " (follow this for THIS supplier's layout):\n"
        + prompt_hint
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_statement(
    ocr_text: str,
    *,
    settings: Settings,
    model_override: str | None = None,
    prompt_hint: str | None = None,
) -> ExtractedStatement:
    """Parse supplier statement OCR text into an ExtractedStatement.

    ``model_override`` lets the ingest layer request the escalation model
    on the second attempt without duplicating call logic.

    ``prompt_hint`` is an optional supplier-specific extraction guidance
    string (sourced from a SupplierStatementTemplate row). When set, it is
    appended to the base system prompt so layout-specific instructions
    supplement rather than replace the generic rules.
    """
    model = model_override or settings.statement_llm_model
    base_url = settings.statement_llm_base
    api_key = settings.statement_llm_api_key

    system_prompt = _build_system_prompt(prompt_hint)

    raw_response = await _call_llm(
        system_prompt,
        ocr_text,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )

    data = _parse_response(raw_response)
    escalated = model_override == settings.statement_llm_model_escalation
    return _build_extracted_statement(data, model_used=model, escalated=escalated)


async def extract_statement_vision(
    image_bytes: bytes,
    mime_type: str,
    *,
    settings: Settings,
    model_override: str | None = None,
    prompt_hint: str | None = None,
) -> ExtractedStatement:
    """Extract a supplier statement from image/binary content via a vision LLM.

    Used as a fallback when Paperless OCR is absent or too short to be
    reliable (fewer than 40 non-whitespace characters). Encodes the binary
    as a base64 data URI and sends it to the configured vision model.

    ``model_override`` lets the ingest layer re-run the vision extraction with
    a stronger escalation model when the first vision pass fails the balance
    gate — without re-downloading the binary from Paperless.

    Note on PDF inputs: most vision models do not accept application/pdf as
    an image_url data URI. When mime_type is 'application/pdf', the call is
    still attempted — the litellm gateway may handle the conversion, or the
    error propagates to the caller. The ingest layer catches RuntimeError /
    JSONDecodeError and persists a NEEDS_REVIEW row rather than 5xx-ing.
    """
    model = model_override or settings.statement_llm_vision_model
    base_url = settings.statement_llm_base
    api_key = settings.statement_llm_api_key

    system_prompt = _build_system_prompt(prompt_hint)

    raw_response = await _call_llm_vision(
        system_prompt,
        image_bytes,
        mime_type,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )

    data = _parse_response(raw_response)
    escalated = model_override is not None
    return _build_extracted_statement(data, model_used=model, escalated=escalated)
