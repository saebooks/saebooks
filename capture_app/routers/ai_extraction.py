"""Module route for AI document extraction — thin shell over
``services.ai_extraction.extract_document``.

The extract endpoint is stateless (no DB, no tenant): it OCRs an uploaded
receipt / invoice / statement via the LiteLLM proxy and returns a structured
dict. The module always has ``CAPTURE_BASE_URL`` unset, so
``extract_document`` runs its real body in-process here (the facade guard in
``services.ai_extraction`` is skipped).

Auth is the module's ``X-Capture-Token`` gate only — the engine already
applied ``require_feature(FLAG_AI_EXTRACTION)`` before delegating, and the
module has no JWT to re-derive the edition from. The MIME allow-list and the
size cap are reused from the engine router so behaviour is identical.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from capture_app.deps import require_capture_token
from saebooks.api.v1.ai_extraction import _MAX_BYTES, _SUPPORTED_MIME_TYPES
from saebooks.services.ai_extraction import (
    AiExtractionNotConfiguredError,
    extract_document,
)

router = APIRouter(
    prefix="/documents",
    tags=["capture-ai-extraction"],
    dependencies=[Depends(require_capture_token)],
)


@router.post("/extract", status_code=status.HTTP_200_OK)
async def extract(
    file: Annotated[UploadFile, File(description="PDF or image to extract data from")],
) -> Any:
    """Upload a document and receive pre-filled accounting form fields.

    Identical response contract to ``api/v1/documents/extract``.
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

    result["extraction_confidence"] = "partial" if result.get("extraction_error") else "ok"
    return result
