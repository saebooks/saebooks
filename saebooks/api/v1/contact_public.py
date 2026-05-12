"""Public contact form endpoint.

POST /api/v1/contact/submit

Accepts name, email, topic, message from anonymous visitors. Inserts into
``contact_messages`` and optionally fires a fire-and-forget notification
to the Telegram bridge via ``CONTACT_NOTIFY_URL``.

Rate limit: 5 submissions per IP per hour (fixed-window, Postgres-backed).
Honeypot: ``website`` field — if non-empty, silently return 200 without
inserting (bot trap).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from saebooks.db import AsyncSessionLocal

logger = logging.getLogger("saebooks.contact")

router = APIRouter(prefix="/contact", tags=["contact"])

# ---------------------------------------------------------------------------
# RFC 5322 light email regex — mirrors signup.py; no email-validator dep.
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# ---------------------------------------------------------------------------
# Hourly rate-limit SQL — same table as signup (rate_limit_counters) but
# keyed by hour window so the factory dep (minute-window) can't be reused.
# ---------------------------------------------------------------------------
_HOURLY_UPSERT = text(
    """
    INSERT INTO rate_limit_counters (scope_key, window_start, count)
    VALUES (:k, date_trunc('hour', now()), 1)
    ON CONFLICT (scope_key, window_start)
    DO UPDATE SET count = rate_limit_counters.count + 1
    RETURNING count
    """
)
_CONTACT_HOURLY_LIMIT = 5


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str
    topic: Literal["general", "enterprise", "support"] = "general"
    message: str = Field(min_length=10, max_length=4000)
    # Honeypot — bots fill this; humans don't see it.
    website: str | None = None

    @field_validator("email")
    @classmethod
    def _v_email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class ContactResponse(BaseModel):
    ok: bool
    id: str


# ---------------------------------------------------------------------------
# Notification helper (fire-and-forget)
# ---------------------------------------------------------------------------


async def _send_notification(
    msg_id: str,
    name: str,
    email: str,
    topic: str,
    message: str,
) -> bool:
    """POST to CONTACT_NOTIFY_URL (Telegram bridge). Returns True on success.

    Never raises — caller wraps in asyncio.create_task; errors are logged.
    """
    notify_url = os.environ.get("CONTACT_NOTIFY_URL", "")
    if not notify_url:
        logger.debug("contact: CONTACT_NOTIFY_URL unset — notification skipped")
        return False
    body = (
        f"New contact form submission\n"
        f"ID: {msg_id}\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Topic: {topic}\n"
        f"Message: {message[:200]}{'...' if len(message) > 200 else ''}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(notify_url, json={"message": body})
        if resp.is_success:
            return True
        logger.warning("contact: notify POST returned %s", resp.status_code)
        return False
    except Exception as exc:
        logger.warning("contact: notify POST failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# POST /contact/submit
# ---------------------------------------------------------------------------


@router.post("/submit")
async def contact_submit(body: ContactRequest, request: Request) -> JSONResponse:
    """Accept a public contact form submission.

    * Honeypot check: if ``website`` is non-empty, return 200 silently.
    * Rate limit: 5/hour per client IP (hourly fixed-window via Postgres).
    * Insert into contact_messages.
    * Fire-and-forget Telegram notification.
    """
    # Honeypot — silent drop
    if body.website:
        logger.info("contact: honeypot triggered, dropping submission")
        fake_id = str(uuid.uuid4())
        return JSONResponse(content={"ok": True, "id": fake_id})

    # Determine client IP. Honours X-Forwarded-For ONLY when
    # SAEBOOKS_TRUST_PROXY_XFF=1 (set in prod compose, behind Caddy).
    # Without the env opt-in, anyone could rotate XFF to defeat the
    # 5/hour contact-form rate limit. Mirrors middleware/rate_limit.py:_client_ip.
    if os.environ.get("SAEBOOKS_TRUST_PROXY_XFF", "").strip() == "1":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            client_ip = xff.split(",", 1)[0].strip() or "0.0.0.0"
        elif request.client is not None:
            client_ip = request.client.host or "0.0.0.0"
        else:
            client_ip = "0.0.0.0"
    elif request.client is not None:
        client_ip = request.client.host or "0.0.0.0"
    else:
        client_ip = "0.0.0.0"

    # Rate limit — 5/hour per IP
    scope_key = f"contact:{client_ip}"
    async with AsyncSessionLocal() as session:
        result = await session.execute(_HOURLY_UPSERT, {"k": scope_key})
        count = int(result.scalar_one())
        await session.commit()

    if count > _CONTACT_HOURLY_LIMIT:
        logger.info("contact: rate limit hit for %s (count=%d)", client_ip, count)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too many contact requests — try again in an hour"},
            headers={"Retry-After": "3600"},
        )

    # Insert the message
    msg_id = str(uuid.uuid4())
    user_agent = request.headers.get("user-agent")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO contact_messages
                    (id, name, email, topic, message, ip, user_agent)
                VALUES
                    (:id, :name, :email, :topic, :message, :ip, :user_agent)
                """
            ),
            {
                "id": msg_id,
                "name": body.name,
                "email": body.email,
                "topic": body.topic,
                "message": body.message,
                "ip": client_ip,
                "user_agent": user_agent,
            },
        )
        await session.commit()

    logger.info("contact: submission %s from %s (topic=%s)", msg_id, client_ip, body.topic)

    # Fire-and-forget notification — wraps _send_notification, updates notified_at on success
    async def _notify_and_update() -> None:
        success = await _send_notification(
            msg_id, body.name, body.email, body.topic, body.message
        )
        if success:
            now = datetime.now(UTC)
            async with AsyncSessionLocal() as upd_session:
                await upd_session.execute(
                    text(
                        "UPDATE contact_messages SET notified_at = :ts WHERE id = :id"
                    ),
                    {"ts": now, "id": msg_id},
                )
                await upd_session.commit()

    asyncio.create_task(_notify_and_update())

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"ok": True, "id": msg_id},
    )


__all__ = ["router"]
