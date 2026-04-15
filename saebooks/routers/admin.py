from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/admin")


@router.get("/settings", response_class=PlainTextResponse)
async def settings_admin() -> str:
    return "TODO: settings admin screen"
