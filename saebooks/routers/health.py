from fastapi import APIRouter

from saebooks.config import settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "edition": settings.edition}
