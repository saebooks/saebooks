import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.services import audit as audit_svc
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
        "group": "GST automation",
        "fields": [
            {
                "key": "gst_auto_post",
                "label": "Auto-post GST lines",
                "type": "choice",
                "choices": ["true", "false"],
                "help": "When on, posting a journal entry auto-generates GST Collected/Paid "
                        "lines. Turn off for full manual control.",
            },
            {
                "key": "gst_collected_account_code",
                "label": "GST Collected account code",
                "type": "str",
                "help": "Account code for GST Collected (liability). Default: 21310.",
            },
            {
                "key": "gst_paid_account_code",
                "label": "GST Paid account code",
                "type": "str",
                "help": "Account code for GST Paid (asset). Default: 21330.",
            },
            {
                "key": "gst_clearing_account_code",
                "label": "GST Clearing account code",
                "type": "str",
                "help": "Account code for BAS settlement clearing. Default: 21320.",
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


@router.get("/audit", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    table_name: str | None = Query(None),
    row_id: str | None = Query(None),
    action: str | None = Query(None),
    performed_by: str | None = Query(None),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    """Browse audit snapshots with filters."""
    page_size = 50
    offset = (page - 1) * page_size

    async with AsyncSessionLocal() as session:
        snapshots = await audit_svc.browse(
            session,
            table_name=table_name or None,
            row_id=row_id or None,
            action=action or None,
            performed_by=performed_by or None,
            limit=page_size + 1,  # fetch one extra to know if there's a next page
            offset=offset,
        )
        tables = await audit_svc.distinct_tables(session)
        actors = await audit_svc.distinct_actors(session)

    has_next = len(snapshots) > page_size
    snapshots = snapshots[:page_size]

    return templates.TemplateResponse(
        request,
        "admin/audit_list.html",
        {
            "edition": app_settings.edition,
            "snapshots": snapshots,
            "tables": tables,
            "actors": actors,
            "filters": {
                "table_name": table_name or "",
                "row_id": row_id or "",
                "action": action or "",
                "performed_by": performed_by or "",
            },
            "page": page,
            "has_next": has_next,
        },
    )


@router.get("/audit/{snapshot_id}", response_class=HTMLResponse)
async def audit_detail(
    request: Request,
    snapshot_id: uuid.UUID,
    reverted: int | None = Query(None),
    revert_error: str | None = Query(None),
) -> HTMLResponse:
    """Show full before/after diff for a single snapshot."""
    async with AsyncSessionLocal() as session:
        snap = await audit_svc.get_snapshot(session, snapshot_id)
        if snap is None:
            return HTMLResponse("Snapshot not found", status_code=404)

        # Also fetch the history for this row so the user can scroll the timeline
        history = await audit_svc.list_snapshots(
            session, snap.table_name, snap.row_id, limit=20
        )

    diff = audit_svc.diff_fields(snap.before_data, snap.after_data)
    revertable = (
        snap.action in audit_svc.REVERTABLE_ACTIONS
        and bool(snap.before_data)
    )

    return templates.TemplateResponse(
        request,
        "admin/audit_detail.html",
        {
            "edition": app_settings.edition,
            "snap": snap,
            "diff": diff,
            "history": history,
            "revertable": revertable,
            "reverted": bool(reverted),
            "revert_error": revert_error,
        },
    )


@router.post("/audit/{snapshot_id}/revert")
async def audit_revert(
    request: Request,
    snapshot_id: uuid.UUID,
) -> RedirectResponse:
    """Apply this snapshot's before-state back to the live row as a new edit."""
    async with AsyncSessionLocal() as session:
        try:
            await audit_svc.revert(
                session, snapshot_id, performed_by="web-revert"
            )
        except audit_svc.RevertError as exc:
            # Bounce back to the detail page with the error in a query string.
            # Keep the error short; the detail page will render it.
            from urllib.parse import quote
            return RedirectResponse(
                f"/admin/audit/{snapshot_id}?revert_error={quote(str(exc))}",
                status_code=303,
            )
    return RedirectResponse(
        f"/admin/audit/{snapshot_id}?reverted=1",
        status_code=303,
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
