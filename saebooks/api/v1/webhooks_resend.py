"""Resend webhook receiver — updates email_send_log with delivery events.

Resend signs webhooks using Svix. The verification header is
``svix-signature`` — a space-separated list of ``v1,<base64-signature>``
tuples — over the canonical string ``<svix-id>.<svix-timestamp>.<body>``
HMAC-SHA256'd with the base64-decoded ``RESEND_WEBHOOK_SECRET``.

If the secret is empty (no webhook configured yet), this endpoint rejects
ALL requests with 503 — fail closed, never accept unverified events.

Events of interest:
    email.sent        — Resend handed the email off to the recipient MTA
    email.delivered   — recipient MTA accepted
    email.bounced     — recipient bounced (hard or soft)
    email.complained  — recipient marked as spam
    email.opened      — open pixel fired
    email.clicked     — link click

Each event updates the relevant timestamp + counter column on the
email_send_log row identified by ``data.email_id`` matching our stored
``resend_message_id``. The raw event is also appended to
``webhook_events`` (JSONB array) so we have a forensic record of every
event ever received for this send — useful for tracking down disputes
or debugging deliverability.

This endpoint is OUTSIDE the tenant-scoped surface: Resend doesn't know
about our tenants. We look up the row by message_id (globally unique),
then derive the tenant from the row itself. RLS is bypassed via
``SET LOCAL app.current_tenant`` from the row's tenant_id immediately
before the UPDATE.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import subprocess
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import text

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal, LoginSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/resend", tags=["webhooks"])


# event type → email_send_log column to stamp
_EVENT_TIMESTAMP_COL: dict[str, str | None] = {
    "email.sent":       None,            # already recorded synchronously
    "email.delivered":  "delivered_at",
    "email.bounced":    "bounced_at",
    "email.opened":     "opened_at",
    "email.clicked":    "clicked_at",
    "email.complained": "complained_at",
}

# Events that fire the Telegram emergency channel — see telegram-bridge
_ALERT_EVENTS = {"email.bounced", "email.complained"}


def _verify_svix_signature(
    body: bytes,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    secret: str,
) -> bool:
    """Verify the svix-signature header against the expected HMAC."""
    if not (svix_id and svix_timestamp and svix_signature):
        return False
    if not secret:
        return False

    # Dev-only escape hatch — only honoured outside production
    if secret == "INSECURE_SKIP" and os.environ.get("SAEBOOKS_ENV", "").lower() in ("dev", "development", "test"):
        logger.warning("RESEND webhook signature SKIPPED (INSECURE_SKIP, dev env)")
        return True

    try:
        # Resend / Svix secrets are formatted "whsec_<base64>"
        key = base64.b64decode(secret.split("_", 1)[-1])
    except Exception:
        return False

    payload = f"{svix_id}.{svix_timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()

    for sig in svix_signature.split():
        version, _, value = sig.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def _claude_notify(message: str) -> None:
    """Best-effort emergency notify via notify-hook (silent failure)."""
    try:
        subprocess.run(
            ["notify-hook", message],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except Exception as exc:
        logger.warning("notify-hook failed: %s", exc)


@router.post("")
async def receive_resend_webhook(
    request: Request,
    svix_id: str | None = Header(default=None, alias="svix-id"),
    svix_timestamp: str | None = Header(default=None, alias="svix-timestamp"),
    svix_signature: str | None = Header(default=None, alias="svix-signature"),
) -> dict[str, Any]:
    """Receive + verify + persist a Resend webhook event.

    NOTE: opens its own DB session (does NOT use get_session) because the
    tenant filter on every other endpoint would block lookup-by-
    message-id here. After matching the row we SET LOCAL app.current_tenant
    from the row's tenant_id before any UPDATE — RLS still satisfied.
    """
    body = await request.body()
    secret = getattr(settings, "resend_webhook_secret", "") or ""

    if not secret:
        logger.error("RESEND webhook received but RESEND_WEBHOOK_SECRET is empty — refusing")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webhook secret not configured")

    if not _verify_svix_signature(body, svix_id, svix_timestamp, svix_signature, secret):
        logger.warning("RESEND webhook signature verification FAILED")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from None

    event_type = event.get("type", "")
    data = event.get("data") or {}
    email_id = data.get("email_id") or data.get("id")
    if not email_id:
        logger.info("RESEND webhook: no email_id in event %s", event_type)
        return {"matched": False, "reason": "no email_id in event"}

    # Lookup runs on the BYPASSRLS owner-role session — same engine used by
    # /auth/login for pre-auth user lookups. Resend doesn't know our tenants,
    # so we have to find the row by global message_id first, then bind the
    # tenant from the row for the WRITE.
    row = None
    async with LoginSessionLocal() as lookup_session:
        row = (await lookup_session.execute(
            text("""
                SELECT id, tenant_id, resend_status
                FROM email_send_log
                WHERE resend_message_id = :mid
                LIMIT 1
            """),
            {"mid": email_id},
        )).first()

    if row is None:
        logger.info("RESEND webhook %s for unknown email_id=%s", event_type, email_id)
        return {"matched": False, "email_id": email_id, "event_type": event_type}

    async with AsyncSessionLocal() as session:
        # Bind tenant for the write — required by FORCE RLS. SET LOCAL
        # doesn't accept parameterised values, so we interpolate the
        # tenant_id we just fetched from the row (already a real UUID; safe).
        session.info["tenant_id"] = str(row.tenant_id)
        # session.info push will trigger the after_begin listener on the
        # NEXT transaction; we also issue SET LOCAL directly for this one.
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{row.tenant_id}'")
        )

        # Always append to webhook_events for forensics
        append_sql = """
            UPDATE email_send_log
            SET webhook_events = COALESCE(webhook_events, '[]'::jsonb) || CAST(:event AS jsonb)
            WHERE id = :id
        """
        await session.execute(
            text(append_sql),
            {"event": json.dumps(event), "id": str(row.id)},
        )

        # Stamp the relevant timestamp / increment counter
        col = _EVENT_TIMESTAMP_COL.get(event_type)
        if col is not None:
            counter = None
            extra = {}
            extra_set = ""
            if event_type == "email.opened":
                counter = "opened_count"
            elif event_type == "email.clicked":
                counter = "clicked_count"
            elif event_type == "email.bounced":
                extra["reason"] = (data.get("bounce") or {}).get("message") or data.get("reason") or ""
                extra_set = ", bounce_reason = :reason"

            sql = f"""
                UPDATE email_send_log
                SET {col} = COALESCE({col}, now()){extra_set}
                {f", {counter} = {counter} + 1" if counter else ""}
                WHERE id = :id
            """
            await session.execute(text(sql), {"id": str(row.id), **extra})

        await session.commit()

    # Emergency notify on deliverability disasters
    if event_type in _ALERT_EVENTS:
        msg = (
            f"🚨 SAE Books email-log alert: {event_type} "
            f"for log_id={row.id} (tenant={row.tenant_id})\n"
            f"Resend id: {email_id}\n"
            f"See /email-log/{row.id} for details."
        )
        _claude_notify(msg)

    return {"matched": True, "log_id": str(row.id), "event_type": event_type}
