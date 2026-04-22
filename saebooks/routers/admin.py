import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select

from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.user import VALID_ROLES, User, UserRole
from saebooks.services import audit as audit_svc
from saebooks.services import backups as backups_svc
from saebooks.services import features as features_svc
from saebooks.services import permissions as perm_svc
from saebooks.services import settings as svc
from saebooks.services import sql_tool as sql_svc
from saebooks.services import theme as theme_svc
from saebooks.services import users as users_svc
from saebooks.services.authz import require_role, require_user
from saebooks.services.licence import (
    has_capacity_for_role_change,
    resolve_licence,
)
from saebooks.services.exports.company import build_company_export
from saebooks.web import templates

router = APIRouter(prefix="/admin")


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


# ---------------------------------------------------------------------------
# Audit CSV export (5-year retention dump).
#
# Registered BEFORE /audit/{snapshot_id} so FastAPI doesn't try to coerce
# the literal "export.csv" into a UUID and 422.
# ---------------------------------------------------------------------------


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    # Accept ISO date (YYYY-MM-DD) or ISO datetime.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


@router.get("/audit/export.csv")
async def audit_export_csv(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    table_name: str | None = Query(None),
    performed_by: str | None = Query(None),
    _admin: User = Depends(require_role(UserRole.ACCOUNTANT)),  # noqa: B008
) -> PlainTextResponse:
    """Download the audit trail as CSV.

    Filters are the same shape as ``/admin/audit``. Timestamps parsed
    with ``datetime.fromisoformat`` so ``2024-07-01`` and
    ``2024-07-01T00:00:00+10:00`` both work.
    """
    async with AsyncSessionLocal() as session:
        csv_text = await audit_svc.export_csv(
            session,
            from_date=_parse_date(from_date),
            to_date=_parse_date(to_date),
            table_name=table_name or None,
            performed_by=performed_by or None,
        )
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="audit-{stamp}.csv"',
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


# ---------------------------------------------------------------------------
# SQL browser — read-only psql-in-browser
# ---------------------------------------------------------------------------


@router.get("/sql", response_class=HTMLResponse)
async def sql_index(
    request: Request,
    q: str | None = Query(None),
    rerun: str | None = Query(None),
) -> HTMLResponse:
    """SQL browser: form + optional query result + history + schema sidebar."""
    async with AsyncSessionLocal() as session:
        tables = await sql_svc.list_tables(session)
        history = await sql_svc.recent_queries(session, limit=20)

        result = None
        error = None
        if rerun:
            try:
                rerun_row = await sql_svc.get_query(
                    session, uuid.UUID(rerun)
                )
                if rerun_row is not None:
                    q = rerun_row.sql
            except (ValueError, TypeError):
                pass

    return templates.TemplateResponse(
        request,
        "admin/sql.html",
        {
            "edition": app_settings.edition,
            "tables": tables,
            "history": history,
            "sql": q or "",
            "result": result,
            "error": error,
            "result_limit": sql_svc.RESULT_LIMIT,
        },
    )


@router.post("/sql", response_class=HTMLResponse)
async def sql_run(
    request: Request,
    sql: str = Form(...),
) -> HTMLResponse:
    """Execute a read-only query and render results."""
    result = None
    error = None
    async with AsyncSessionLocal() as session:
        try:
            result = await sql_svc.run_query(
                session, sql, performed_by="web"
            )
        except sql_svc.QueryError as exc:
            error = str(exc)

        tables = await sql_svc.list_tables(session)
        history = await sql_svc.recent_queries(session, limit=20)

    return templates.TemplateResponse(
        request,
        "admin/sql.html",
        {
            "edition": app_settings.edition,
            "tables": tables,
            "history": history,
            "sql": sql,
            "result": result,
            "error": error,
            "result_limit": sql_svc.RESULT_LIMIT,
        },
    )


@router.post("/sql/export", response_class=PlainTextResponse)
async def sql_export(
    request: Request,
    sql: str = Form(...),
) -> PlainTextResponse:
    """Run a query and return the whole result (no 500-row cap) as CSV.

    The cap still applies here — CSV is generated from the same capped
    result. If you need more rows than the cap, raise RESULT_LIMIT in the
    service and re-run.
    """
    async with AsyncSessionLocal() as session:
        try:
            result = await sql_svc.run_query(
                session, sql, performed_by="web-csv"
            )
        except sql_svc.QueryError as exc:
            return PlainTextResponse(
                f"Error: {exc}\n",
                status_code=400,
                media_type="text/plain; charset=utf-8",
            )
    csv_text = sql_svc.to_csv(result.columns, result.rows)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="query.csv"'},
    )


@router.get("/license", response_class=HTMLResponse)
async def license_admin(request: Request) -> HTMLResponse:
    """Show the active edition and the per-feature flag matrix.

    Read-only for now — future batches may add a paste-in licence-key
    box when we introduce JWT-backed flags.
    """
    return templates.TemplateResponse(
        request,
        "admin/license.html",
        {
            "edition": app_settings.edition,
            "flags": features_svc.active_flags(),
        },
    )


# ---------------------------------------------------------------------------
# Theme selector (Batch QQ)
# ---------------------------------------------------------------------------


@router.get("/theme", response_class=HTMLResponse)
async def theme_admin(
    request: Request,
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
    saved: str | None = Query(default=None),
    err: str | None = Query(default=None),
) -> HTMLResponse:
    """Show the active theme + radio-list of themes an admin can pick.

    The selection here is the *company-wide* theme — what every
    anonymous request + every user without a ``preferred_theme`` sees.
    A restart is required for the Jinja ``ChoiceLoader`` to pick up the
    change (the CSS bundle switches immediately, but any theme-level
    template override only takes effect after app reload).
    """
    async with AsyncSessionLocal() as session:
        db_setting = await svc.get(session, "theme")
    db_theme = db_setting.get("name") if isinstance(db_setting, dict) else None
    active = theme_svc.resolve_theme(
        app_settings, db_setting=db_theme
    )
    return templates.TemplateResponse(
        request,
        "admin/theme.html",
        {
            "edition": app_settings.edition,
            "active": active,
            "themes": sorted(theme_svc.ACTIVE_THEMES),
            "env_override": bool(app_settings.frontend and app_settings.frontend != theme_svc.DEFAULT_THEME),
            "saved": saved,
            "err": err,
        },
    )


@router.post("/theme")
async def theme_admin_save(
    request: Request,
    theme: str = Form(...),
    user: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    """Persist the chosen theme in the ``settings`` table.

    Validates against :data:`services.theme.ACTIVE_THEMES` up front so
    the form can never poison the DB with a typo. Needs an app restart
    to flip the Jinja loader chain.
    """
    try:
        canonical = theme_svc.validate_startup_theme(theme)
    except theme_svc.ThemeError:
        return RedirectResponse("/admin/theme?err=bad_theme", status_code=303)
    async with AsyncSessionLocal() as session:
        await svc.set(
            session,
            "theme",
            {"name": canonical},
            updated_by=user.username,
        )
    return RedirectResponse("/admin/theme?saved=1", status_code=303)


@router.get("/backups", response_class=HTMLResponse)
async def backups_admin(request: Request) -> HTMLResponse:
    """List pg_dump backups and recent backup/restore-test runs."""
    return templates.TemplateResponse(
        request,
        "admin/backups.html",
        {
            "edition": app_settings.edition,
            "summary": backups_svc.summary(),
            "dumps": backups_svc.list_dumps(),
            "runs": backups_svc.recent_backup_runs(limit=30),
            "tests": backups_svc.recent_restore_tests(limit=20),
        },
    )


# ---------------------------------------------------------------------------
# Users (role admin)
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> HTMLResponse:
    """List every Authentik-authenticated user that's hit the app."""
    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(
                select(User).order_by(User.username)
            )
        ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/users_list.html",
        {
            "edition": app_settings.edition,
            "users": users,
            "valid_roles": sorted(VALID_ROLES),
        },
    )


@router.post("/users/{user_id}/role")
async def users_set_role(
    user_id: uuid.UUID,
    role: str = Form(...),
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    """Change a user's role. 400 on unknown roles.

    Enforces the CHARTER §12.2 seat cap for the active edition — a
    promotion from an employee role to ``admin`` is blocked when the
    admin bucket is already full on a hard-cap tier, and warned-on
    (but allowed) on Offline's soft cap. Demotions check employee
    capacity symmetrically so a tier with a full employee bucket
    can't silently overflow.
    """
    if role not in VALID_ROLES:
        return RedirectResponse("/admin/users?err=bad_role", status_code=303)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users?err=not_found", status_code=303)

        from_seat = users_svc.seat_class_for(user.role)
        to_seat = users_svc.seat_class_for(role)
        if from_seat != to_seat:
            current_admins = await users_svc.count_admin_seats(session)
            current_employees = await users_svc.count_employee_seats(session)
            licence = resolve_licence()
            check = has_capacity_for_role_change(
                edition=licence.edition,
                current_admins=current_admins,
                current_employees=current_employees,
                from_role=from_seat,
                to_role=to_seat,
            )
            if check.blocked:
                return RedirectResponse(
                    f"/admin/users?err=seat_cap&seat={to_seat}",
                    status_code=303,
                )
            if check.should_warn:
                # Offline soft cap — allow but surface the banner on
                # the redirect target (the users list template reads
                # the ``warn`` query param).
                user.role = role
                await session.commit()
                return RedirectResponse(
                    f"/admin/users?saved=1&warn=seat_soft&seat={to_seat}",
                    status_code=303,
                )

        user.role = role
        await session.commit()
    return RedirectResponse("/admin/users?saved=1", status_code=303)


@router.post("/users/{user_id}/archive")
async def users_archive(
    user_id: uuid.UUID,
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users?err=not_found", status_code=303)
        user.archived_at = datetime.now()
        await session.commit()
    return RedirectResponse("/admin/users?archived=1", status_code=303)


@router.post("/users/{user_id}/unarchive")
async def users_unarchive(
    user_id: uuid.UUID,
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users?err=not_found", status_code=303)
        user.archived_at = None
        await session.commit()
    return RedirectResponse("/admin/users?unarchived=1", status_code=303)


# ---------------------------------------------------------------------------
# Permissions matrix (admin-only)
# ---------------------------------------------------------------------------


@router.get("/permissions", response_class=HTMLResponse)
async def permissions_matrix(
    request: Request,
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> HTMLResponse:
    """Render the role x permission matrix + per-user overrides.

    Admin ticks/unticks cells, POSTs back to /admin/permissions/role to
    update grants. Same page also lets admin set a per-user override.
    """
    async with AsyncSessionLocal() as session:
        all_perms = await perm_svc.all_permissions(session)
        grants: dict[str, frozenset[str]] = {}
        for role in sorted(VALID_ROLES):
            grants[role] = await perm_svc.role_grants(session, role)
        users = (
            await session.execute(
                select(User)
                .where(User.archived_at.is_(None))
                .order_by(User.username)
            )
        ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/permissions_matrix.html",
        {
            "edition": app_settings.edition,
            "permissions": all_perms,
            "roles": sorted(VALID_ROLES),
            "grants": grants,
            "users": users,
        },
    )


@router.post("/permissions/role")
async def permissions_set_role(
    request: Request,
    role: str = Form(...),
    _admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    """Replace the grant-set for ``role`` with the checked codes.

    Form shape: any number of ``code=<slug>`` fields. Codes not
    present in the form are dropped from the role.
    """
    if role not in VALID_ROLES:
        return RedirectResponse(
            "/admin/permissions?err=bad_role", status_code=303
        )
    form = await request.form()
    codes = [str(v) for v in form.getlist("code")]
    async with AsyncSessionLocal() as session:
        await perm_svc.set_role_grants(session, role, codes)
    return RedirectResponse(
        f"/admin/permissions?saved={role}", status_code=303
    )


@router.post("/permissions/user")
async def permissions_user_override(
    user_id: uuid.UUID = Form(...),  # noqa: B008
    code: str = Form(...),
    action: str = Form(...),
    admin: User = Depends(require_role(UserRole.ADMIN)),  # noqa: B008
) -> RedirectResponse:
    """Grant / revoke / clear a per-user override.

    ``action`` ∈ {``grant``, ``revoke``, ``clear``}. Grant and revoke
    upsert into ``user_permissions`` with the appropriate boolean; clear
    deletes the row so the user falls back to the role grant.
    """
    async with AsyncSessionLocal() as session:
        if action == "clear":
            await perm_svc.revoke_user_override(session, user_id, code)
        elif action == "grant":
            await perm_svc.grant_user_permission(
                session,
                user_id,
                code,
                granted=True,
                granted_by=admin.username,
            )
        elif action == "revoke":
            await perm_svc.grant_user_permission(
                session,
                user_id,
                code,
                granted=False,
                granted_by=admin.username,
            )
        else:
            return RedirectResponse(
                "/admin/permissions?err=bad_action", status_code=303
            )
    return RedirectResponse(
        f"/admin/permissions?user_saved={user_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# /whoami — self-service identity check for any authenticated user
# ---------------------------------------------------------------------------


@router.get("/whoami")
async def whoami(
    request: Request,
    user: User = Depends(require_user()),  # noqa: B008
) -> dict[str, str | None]:
    """Return the caller's user row + role. Useful for /debug and /tests."""
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
    }


# ---------------------------------------------------------------------------
# Full-company ZIP export
# ---------------------------------------------------------------------------


@router.get("/company/export", response_class=HTMLResponse)
async def company_export_form(
    request: Request,
    _admin: User = Depends(require_role(UserRole.ACCOUNTANT)),  # noqa: B008
) -> HTMLResponse:
    """Pick a company to export. Most Community installs have exactly one."""
    async with AsyncSessionLocal() as session:
        companies = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.name)
            )
        ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/company_export.html",
        {
            "edition": app_settings.edition,
            "companies": companies,
        },
    )


@router.post("/company/export")
async def company_export_zip(
    request: Request,
    company_id: uuid.UUID = Form(...),  # noqa: B008
    include_audit: str = Form("on"),
    admin_user: User = Depends(require_role(UserRole.ACCOUNTANT)),  # noqa: B008
) -> Response:
    """Stream the full-company zip bundle."""
    async with AsyncSessionLocal() as session:
        try:
            payload, filename = await build_company_export(
                session,
                company_id=company_id,
                exported_by=admin_user.username,
                include_audit=(include_audit == "on"),
            )
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
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
