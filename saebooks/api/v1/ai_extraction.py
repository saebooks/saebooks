"""JSON router — ``/api/v1/documents/extract``.

AI document extraction endpoint. Accepts a multipart file upload
(receipt, supplier invoice, bank statement) and returns structured
accounting data extracted by a vision-capable LLM behind any
OpenAI-compatible API (configured via ``LITELLM_BASE_URL`` +
``LITELLM_VISION_MODEL``).

Feature-gated to Business+ (``FLAG_AI_EXTRACTION``). Community and
Offline editions receive 404, consistent with the CHARTER §6 pattern
that lower-tier endpoints simply don't exist from the outside.

Supported MIME types
--------------------
* image/jpeg
* image/png
* image/webp
* application/pdf

Unsupported types return 422 Unprocessable Entity.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from saebooks.api.v1.auth import require_bearer
from saebooks.services.ai_extraction import (
    AiExtractionNotConfiguredError,
    extract_document,
)
from saebooks.services.features import FLAG_AI_EXTRACTION, require_feature

_SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
})

# Maximum upload size: 10 MiB. The LiteLLM proxy and underlying model
# can handle larger files but we cap here to avoid holding giant payloads in memory.
_MAX_BYTES = 10 * 1024 * 1024

router = APIRouter(
    prefix="/documents",
    tags=["ai_extraction"],
    dependencies=[
        Depends(require_bearer),
        Depends(require_feature(FLAG_AI_EXTRACTION)),
    ],
)


@router.post(
    "/extract",
    summary="Extract structured data from a document (receipt / invoice / statement)",
    status_code=status.HTTP_200_OK,
)
async def extract(
    file: Annotated[UploadFile, File(description="PDF or image to extract data from")],
) -> Any:
    """Upload a document and receive pre-filled accounting form fields.

    Returns a JSON object with:

    * ``vendor_name``, ``invoice_number``, ``date``, ``due_date``
    * ``subtotal``, ``tax_amount``, ``total``, ``currency``
    * ``line_items`` — list of ``{description, qty, unit_price, amount, tax_code}``
    * ``notes``
    * ``extraction_confidence`` — ``"ok"`` on success, ``"partial"`` on API error
    * ``extraction_error`` — error message string on partial result, else ``null``
    """
    mime_type = file.content_type or ""
    if mime_type not in _SUPPORTED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file type '{mime_type}'. "
                f"Accepted: {', '.join(sorted(_SUPPORTED_MIME_TYPES))}"
            ),
        )

    file_bytes = await file.read()
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File too large ({len(file_bytes)} bytes); maximum is {_MAX_BYTES} bytes.",
        )

    try:
        result = await extract_document(file_bytes, mime_type)
    except AiExtractionNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    confidence = "partial" if result.get("extraction_error") else "ok"
    result["extraction_confidence"] = confidence
    return result
