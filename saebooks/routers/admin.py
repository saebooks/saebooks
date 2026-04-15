from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import settings as svc

router = APIRouter(prefix="/admin")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Settings that the admin screen exposes, grouped for display.
SETTINGS_SCHEMA: list[dict[str, object]] = [
    {
        "group": "Financial year",
        "fields": [
            {"key": "fin_year_start_month", "label": "FY start month (1-12)", "type": "int"},
            {"key": "base_currency", "label": "Base currency", "type": "str"},
        ],
    },
    {
        "group": "GST / VAT rounding",
        "fields": [
            {
                "key": "gst_rounding_sales",
                "label": "Sales rounding",
                "type": "choice",
                "choices": ["DOWN", "UP", "HALF_UP"],
            },
            {
                "key": "gst_rounding_purchases",
                "label": "Purchases rounding",
                "type": "choice",
                "choices": ["DOWN", "UP", "HALF_UP"],
            },
            {
                "key": "gst_calc_level",
                "label": "Calculation level",
                "type": "choice",
                "choices": ["LINE", "TOTAL"],
            },
        ],
    },
    {
        "group": "Chart of accounts",
        "fields": [
            {
                "key": "prefix_mode",
                "label": "Account numbering",
                "type": "choice",
                "choices": ["classic", "extended"],
                "help": "Classic = single-digit prefixes (1-9, MYOB-style). "
                        "Extended = multi-digit prefixes (10, 200, etc.).",
            },
            {
                "key": "structured_numbering",
                "label": "Enforce numbering structure",
                "type": "choice",
                "choices": ["true", "false"],
                "help": "When on, account codes are validated against defined ranges. "
                        "Turn off for freeform codes.",
            },
        ],
    },
    {
        "group": "Audit",
        "fields": [
            {
                "key": "audit_mode",
                "label": "Audit mode",
                "type": "choice",
                "choices": ["immutable", "open_journal", "hybrid"],
            },
        ],
    },
    {
        "group": "Retention",
        "fields": [
            {"key": "retention_years_journal", "label": "Journal retention (years)", "type": "int"},
            {
                "key": "retention_years_attachments",
                "label": "Attachment retention (years)",
                "type": "int",
            },
        ],
    },
]


@router.get("/settings", response_class=HTMLResponse)
async def settings_admin(request: Request) -> HTMLResponse:
    async with AsyncSessionLocal() as session:
        current = await svc.all(session)
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "edition": app_settings.edition,
            "schema": SETTINGS_SCHEMA,
            "current": current,
            "saved": False,
        },
    )


@router.post("/settings")
async def settings_save(request: Request) -> HTMLResponse:
    form = dict(await request.form())
    async with AsyncSessionLocal() as session:
        for group in SETTINGS_SCHEMA:
            fields: object = group["fields"]
            assert isinstance(fields, list)
            for item in fields:
                assert isinstance(item, dict)
                key = str(item["key"])
                raw = str(form.get(key, ""))
                item_type = str(item["type"])
                if item_type == "int":
                    value: object = int(raw) if raw else 0
                else:
                    value = raw
                await svc.set(session, str(key), value, updated_by="admin")
        current = await svc.all(session)
    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "edition": app_settings.edition,
            "schema": SETTINGS_SCHEMA,
            "current": current,
            "saved": True,
        },
    )
