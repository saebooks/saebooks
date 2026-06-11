"""Create Outlook DRAFT messages via Microsoft Graph.

Application-permission (client_credentials) flow against the Azure AD app
configured in ``settings.graph_*``. Used by the customer_email DRAFT MODE:
the engine composes the customer-facing email and parks it in the
operator's Drafts folder for human review and a manual send from Outlook.

This module NEVER sends mail. It only creates drafts
(``POST /users/{mailbox}/messages`` — isDraft is implicit), so it needs
only Mail.ReadWrite, not Mail.Send.

Failure policy: ``create_outlook_draft`` never raises on config, auth, or
network problems — it returns ``DraftResult.error`` so the caller can
record a failed status in the email_send_log audit trail and surface the
reason to the UI.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

import httpx

from saebooks.config import settings

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"

# Module-level token cache: (access_token, expiry_epoch). Application
# tokens are mailbox-independent so one cache entry suffices.
_token_cache: tuple[str, float] | None = None


class GraphConfigError(RuntimeError):
    """GRAPH_* settings missing or the token endpoint refused us."""


@dataclass(frozen=True)
class DraftResult:
    draft_id: str | None
    web_link: str | None
    error: str | None


async def _get_token() -> str:
    """Fetch (or reuse) a client_credentials access token."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time() + 60:
        return _token_cache[0]

    if not (
        settings.graph_tenant_id
        and settings.graph_client_id
        and settings.graph_client_secret
    ):
        raise GraphConfigError(
            "GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET not configured"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_LOGIN_BASE}/{settings.graph_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    if resp.status_code != 200:
        raise GraphConfigError(
            f"Graph token endpoint {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    token = str(data["access_token"])
    _token_cache = (token, time.time() + int(data.get("expires_in", 3600)))
    return token


async def create_outlook_draft(
    *,
    mailbox: str,
    subject: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    body_html: str,
    attachments: list[tuple[str, bytes, str]],
) -> DraftResult:
    """Create a draft (with inline fileAttachments) in ``mailbox``.

    ``attachments`` is a list of ``(filename, content, content_type)``.
    Inline base64 attachments are fine for the PDF sizes we produce
    (Graph allows up to ~3 MB per attachment on this path).
    """
    if not mailbox:
        return DraftResult(None, None, "GRAPH_DRAFT_MAILBOX not configured")

    try:
        token = await _get_token()
    except (GraphConfigError, httpx.HTTPError) as exc:
        return DraftResult(None, None, f"Graph auth failed: {exc}")

    def _addrs(vals: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in vals]

    payload: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": _addrs(to),
    }
    if cc:
        payload["ccRecipients"] = _addrs(cc)
    if bcc:
        payload["bccRecipients"] = _addrs(bcc)
    if attachments:
        payload["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": content_type,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
            for (filename, content, content_type) in attachments
        ]

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/users/{mailbox}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return DraftResult(None, None, f"Graph network error: {exc!r}")

    if resp.status_code != 201:
        return DraftResult(None, None, f"Graph {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    logger.info(
        "outlook draft created in %s: %s", mailbox, data.get("id", "")[:32]
    )
    return DraftResult(data.get("id"), data.get("webLink"), None)
