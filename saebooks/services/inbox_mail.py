"""Document Inbox email-in — mail transport + the poller walk (spec §4).

Per-tenant addresses ``<token>@in.saebooks.com.au`` all land in ONE
catch-all mailbox, drained by ``python -m saebooks.cli inbox-poll-mail``
on a short cron — a **poller, never an inbound SMTP daemon** (no
long-running worker by engine design). Transport sits behind the
:class:`MailSource` protocol with two adapters — plain IMAP (stdlib
``imaplib``, self-hosted operators bring their own mailbox) and
Microsoft Graph (``httpx`` against the REST API, app-only
client-credentials flow) — selected by ``SAEBOOKS_INBOX_MAIL_PROVIDER``.
Wiring the live mailbox is pure env config; nothing here hardcodes a
host or a credential.

Poller flow per message (the walk lives in :func:`poll_mailbox`),
running as the NOBYPASSRLS ``saebooks_app`` cross-tenant walker with the
routing map from the SECURITY DEFINER enumerator (migration 0176):

* resolve token → tenant; miss → move to the Quarantine folder,
  **no bounce ever** (the no-outgoing-customer-email rule is absolute);
* ``SET LOCAL app.current_tenant`` per transaction;
* per-tenant daily quota (config, default 200 EMAIL documents/day) —
  excess messages quarantined;
* **attachments first, ledger row last**: each qualifying attachment
  (≤10 MiB, MIME whitelist, inline images under 20 KiB silently
  skipped; oversize/wrong-type counted in ``skipped_count``, never
  ingested — no document row exists without a blob) goes through
  :func:`document_inbox.ingest_email_attachment` with
  ``source_ref='<message-id>#<n>'`` and commits per attachment;
* then the ledger row; then the message moves to Processed.

A crash mid-message replays it: completed attachments hit the
``source_ref`` unique and skip; remaining attachments get processed —
no silent loss. Byte-duplicates land as DUPLICATE rows. Body-only mail
is recorded with ``document_count=0`` and filed to Processed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import email.policy
import hashlib
import imaplib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.models.inbox_email import InboxEmailMessage
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import vault as vault_client

logger = logging.getLogger("saebooks.services.inbox_mail")

# Identical to the upload surface by design — nothing ingested is
# unextractable (spec §3/§4).
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
})
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# Inline images under this size are signature logos / tracking pixels —
# silently skipped, not even counted (spec §4).
INLINE_SKIP_BYTES = 20 * 1024


class MailNotConfiguredError(RuntimeError):
    """SAEBOOKS_INBOX_MAIL_PROVIDER unset/unknown or its adapter is
    missing required settings — named vars only, never values."""


# ---------------------------------------------------------------------------
# Transport-neutral message shape + the MailSource protocol
# ---------------------------------------------------------------------------


@dataclass
class MailAttachment:
    filename: str
    mime: str
    data: bytes
    inline: bool = False


@dataclass
class MailMessage:
    handle: str  # adapter-opaque id (IMAP UID / Graph message id)
    message_id: str  # RFC 5322 Message-ID
    from_addr: str
    subject: str
    recipients: list[str]
    received_at: datetime | None
    attachments: list[MailAttachment] = field(default_factory=list)


class MailSource(Protocol):
    """One catch-all mailbox, pollable and folder-movable."""

    mailbox: str  # ledger identity (IMAP username / Graph mailbox UPN)

    async def list_messages(self) -> list[str]:
        """Handles of every message currently in the inbox folder."""
        ...

    async def fetch(self, handle: str) -> MailMessage: ...

    async def move(self, handle: str, folder: str) -> None:
        """File the message into ``folder`` (created if missing)."""
        ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# RFC 822 parsing (shared by the IMAP adapter; unit-testable standalone)
# ---------------------------------------------------------------------------

_RECIPIENT_HEADERS = (
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "Resent-To",
)


def parse_rfc822(raw: bytes, handle: str) -> MailMessage:
    """Parse raw RFC 822 bytes into the transport-neutral shape."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # A message with no Message-ID still needs a stable replay key.
        message_id = f"<no-message-id-{hashlib.sha256(raw).hexdigest()[:24]}>"

    recipients: list[str] = []
    for header in _RECIPIENT_HEADERS:
        values = msg.get_all(header) or []
        recipients.extend(
            addr for _name, addr in getaddresses(values) if addr
        )

    received_at: datetime | None = None
    date_header = msg.get("Date")
    if date_header:
        try:
            received_at = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            received_at = None

    return MailMessage(
        handle=handle,
        message_id=message_id,
        from_addr=parseaddr(msg.get("From") or "")[1],
        subject=str(msg.get("Subject") or ""),
        recipients=recipients,
        received_at=received_at,
        attachments=_walk_attachments(msg),
    )


def _walk_attachments(msg: Message) -> list[MailAttachment]:
    out: list[MailAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = part.get("Content-ID")
        # Plain body text/html without a filename is the message body,
        # not an attachment.
        if disposition not in ("attachment", "inline") and not filename:
            continue
        if disposition is None and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        inline = disposition == "inline" or (
            content_id is not None and disposition != "attachment"
        )
        out.append(
            MailAttachment(
                filename=filename or "attachment",
                mime=part.get_content_type().lower(),
                data=payload,
                inline=inline,
            )
        )
    return out


# ---------------------------------------------------------------------------
# IMAP adapter — stdlib imaplib, sync calls wrapped in a worker thread
# ---------------------------------------------------------------------------


class ImapMailSource:
    """Plain-IMAP catch-all mailbox (self-hosted operators bring their
    own). All ``imaplib`` calls run via ``asyncio.to_thread`` — the CLI
    is a short-lived cron process, not a hot path."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 993,
        username: str,
        password: str,
        use_ssl: bool = True,
        folder: str = "INBOX",
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._folder = folder
        self._timeout = timeout
        self._conn: imaplib.IMAP4 | None = None
        self.mailbox = username

    # -- sync internals (run inside to_thread) ---------------------------

    def _connect_sync(self) -> imaplib.IMAP4:
        if self._conn is not None:
            return self._conn
        conn: imaplib.IMAP4
        if self._use_ssl:
            conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=self._timeout)
        else:
            conn = imaplib.IMAP4(self._host, self._port, timeout=self._timeout)
        conn.login(self._username, self._password)
        conn.select(self._folder)
        self._conn = conn
        return conn

    def _list_sync(self) -> list[str]:
        conn = self._connect_sync()
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {status}")
        return [uid.decode("ascii") for uid in (data[0] or b"").split()]

    def _fetch_sync(self, handle: str) -> bytes:
        conn = self._connect_sync()
        status, data = conn.uid("FETCH", handle, "(RFC822)")
        if status != "OK" or not data or data[0] is None:
            raise RuntimeError(f"IMAP FETCH {handle} failed: {status}")
        # data[0] is (envelope, bytes) for a successful literal fetch.
        return data[0][1]

    def _move_sync(self, handle: str, folder: str) -> None:
        conn = self._connect_sync()
        # Create-if-missing is idempotent; failure means it exists.
        with contextlib.suppress(imaplib.IMAP4.error):  # pragma: no cover
            conn.create(folder)
        # Prefer RFC 6851 MOVE; fall back to COPY + delete + EXPUNGE.
        try:
            status, _ = conn.uid("MOVE", handle, folder)
        except imaplib.IMAP4.error:
            status = "NO"
        if status != "OK":
            status, _ = conn.uid("COPY", handle, folder)
            if status != "OK":
                raise RuntimeError(f"IMAP COPY {handle} -> {folder} failed")
            conn.uid("STORE", handle, "+FLAGS", r"(\Deleted)")
            conn.expunge()

    def _close_sync(self) -> None:
        if self._conn is None:
            return
        with contextlib.suppress(Exception):  # pragma: no cover — teardown
            self._conn.logout()
        self._conn = None

    # -- async protocol surface ------------------------------------------

    async def list_messages(self) -> list[str]:
        return await asyncio.to_thread(self._list_sync)

    async def fetch(self, handle: str) -> MailMessage:
        raw = await asyncio.to_thread(self._fetch_sync, handle)
        return parse_rfc822(raw, handle)

    async def move(self, handle: str, folder: str) -> None:
        await asyncio.to_thread(self._move_sync, handle, folder)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)


# ---------------------------------------------------------------------------
# Microsoft Graph adapter — httpx, app-only client-credentials flow
# ---------------------------------------------------------------------------


class GraphMailSource:
    """Microsoft Graph mailbox drain (M365 shared-mailbox option for the
    hosted deployment). App registration needs application-permission
    ``Mail.ReadWrite`` admin-consented and scoped to the catch-all
    mailbox by an application access policy. Config carries credential
    NAMES only — values live in env."""

    _LOGIN_BASE = "https://login.microsoftonline.com"

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout: float = 30.0,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._base = base_url.rstrip("/")
        self.mailbox = mailbox
        self._client = httpx.AsyncClient(timeout=timeout)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._folder_ids: dict[str, str] = {}

    async def _bearer(self) -> dict[str, str]:
        if self._access_token is None or time.monotonic() >= self._token_expires_at:
            resp = await self._client.post(
                f"{self._LOGIN_BASE}/{self._tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._access_token = body["access_token"]
            # Refresh a minute early.
            self._token_expires_at = (
                time.monotonic() + int(body.get("expires_in", 3600)) - 60
            )
        return {"Authorization": f"Bearer {self._access_token}"}

    def _url(self, path: str) -> str:
        return f"{self._base}/users/{self.mailbox}{path}"

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._client.get(
            self._url(path), headers=await self._bearer(), params=params or None
        )
        resp.raise_for_status()
        return resp.json()

    async def list_messages(self) -> list[str]:
        body = await self._get(
            "/mailFolders/inbox/messages", **{"$top": 50, "$select": "id"}
        )
        return [m["id"] for m in body.get("value", [])]

    async def fetch(self, handle: str) -> MailMessage:
        detail = await self._get(
            f"/messages/{handle}",
            **{
                "$select": (
                    "id,internetMessageId,subject,from,receivedDateTime,"
                    "toRecipients,ccRecipients,hasAttachments"
                )
            },
        )
        recipients = [
            r["emailAddress"]["address"]
            for key in ("toRecipients", "ccRecipients")
            for r in detail.get(key, [])
            if r.get("emailAddress", {}).get("address")
        ]
        received_at: datetime | None = None
        if detail.get("receivedDateTime"):
            try:
                received_at = datetime.fromisoformat(
                    detail["receivedDateTime"].replace("Z", "+00:00")
                )
            except ValueError:
                received_at = None

        attachments: list[MailAttachment] = []
        if detail.get("hasAttachments"):
            atts = await self._get(f"/messages/{handle}/attachments")
            for att in atts.get("value", []):
                # Only fileAttachment carries bytes; item/reference
                # attachments surface as unsupported (skipped+counted).
                data = b""
                if att.get("@odata.type") == "#microsoft.graph.fileAttachment":
                    data = base64.b64decode(att.get("contentBytes") or "")
                attachments.append(
                    MailAttachment(
                        filename=att.get("name") or "attachment",
                        mime=(att.get("contentType") or "application/octet-stream").lower(),
                        data=data,
                        inline=bool(att.get("isInline")),
                    )
                )

        message_id = (detail.get("internetMessageId") or "").strip()
        if not message_id:
            message_id = f"<graph-{detail['id']}>"
        return MailMessage(
            handle=handle,
            message_id=message_id,
            from_addr=(
                detail.get("from", {})
                .get("emailAddress", {})
                .get("address", "")
            ),
            subject=detail.get("subject") or "",
            recipients=recipients,
            received_at=received_at,
            attachments=attachments,
        )

    async def _folder_id(self, folder: str) -> str:
        if folder in self._folder_ids:
            return self._folder_ids[folder]
        body = await self._get(
            "/mailFolders", **{"$filter": f"displayName eq '{folder}'"}
        )
        values = body.get("value", [])
        if values:
            fid = values[0]["id"]
        else:
            resp = await self._client.post(
                self._url("/mailFolders"),
                headers=await self._bearer(),
                json={"displayName": folder},
            )
            resp.raise_for_status()
            fid = resp.json()["id"]
        self._folder_ids[folder] = fid
        return fid

    async def move(self, handle: str, folder: str) -> None:
        destination = await self._folder_id(folder)
        resp = await self._client.post(
            self._url(f"/messages/{handle}/move"),
            headers=await self._bearer(),
            json={"destinationId": destination},
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Adapter selection — pure env config
# ---------------------------------------------------------------------------


def mail_source_from_settings(settings: Settings | None = None) -> MailSource:
    """Build the configured adapter, or raise
    :class:`MailNotConfiguredError` naming the missing env vars (names
    only — never values)."""
    settings = settings or _default_settings
    provider = (settings.inbox_mail_provider or "").strip().lower()
    if provider == "imap":
        missing = [
            name
            for name, value in (
                ("SAEBOOKS_INBOX_IMAP_HOST", settings.inbox_imap_host),
                ("SAEBOOKS_INBOX_IMAP_USERNAME", settings.inbox_imap_username),
                ("SAEBOOKS_INBOX_IMAP_PASSWORD", settings.inbox_imap_password),
            )
            if not value
        ]
        if missing:
            raise MailNotConfiguredError(
                f"IMAP mail source selected but unset: {', '.join(missing)}"
            )
        return ImapMailSource(
            host=settings.inbox_imap_host,
            port=settings.inbox_imap_port,
            username=settings.inbox_imap_username,
            password=settings.inbox_imap_password,
            use_ssl=settings.inbox_imap_use_ssl,
            folder=settings.inbox_imap_folder,
        )
    if provider == "graph":
        missing = [
            name
            for name, value in (
                ("SAEBOOKS_INBOX_GRAPH_TENANT_ID", settings.inbox_graph_tenant_id),
                ("SAEBOOKS_INBOX_GRAPH_CLIENT_ID", settings.inbox_graph_client_id),
                (
                    "SAEBOOKS_INBOX_GRAPH_CLIENT_SECRET",
                    settings.inbox_graph_client_secret,
                ),
                ("SAEBOOKS_INBOX_GRAPH_MAILBOX", settings.inbox_graph_mailbox),
            )
            if not value
        ]
        if missing:
            raise MailNotConfiguredError(
                f"Graph mail source selected but unset: {', '.join(missing)}"
            )
        return GraphMailSource(
            tenant_id=settings.inbox_graph_tenant_id,
            client_id=settings.inbox_graph_client_id,
            client_secret=settings.inbox_graph_client_secret,
            mailbox=settings.inbox_graph_mailbox,
            base_url=settings.inbox_graph_base_url,
        )
    raise MailNotConfiguredError(
        "SAEBOOKS_INBOX_MAIL_PROVIDER must be 'imap' or 'graph' to poll mail "
        f"(current: {provider or 'unset'})"
    )


# ---------------------------------------------------------------------------
# Token routing
# ---------------------------------------------------------------------------

RouteMap = dict[str, tuple[uuid.UUID, uuid.UUID | None]]


async def load_routing_map(session: AsyncSession) -> RouteMap:
    """token → (tenant_id, company_id) for every ACTIVE address, via the
    SECURITY DEFINER enumerator (migration 0176) so the NOBYPASSRLS
    poller role sees the whole map with no tenant GUC."""
    rows = (
        await session.execute(
            text(
                "SELECT token, tenant_id, company_id "
                "FROM inbox_email_addresses_for_poll()"
            )
        )
    ).all()
    return {row.token: (row.tenant_id, row.company_id) for row in rows}


def candidate_tokens(recipients: list[str], domain: str) -> list[str]:
    """Local-parts of recipient addresses, filtered to the ingestion
    domain when configured (empty domain = accept any — dev only)."""
    domain = (domain or "").strip().lower()
    out: list[str] = []
    for addr in recipients:
        addr = (addr or "").strip().lower()
        local, sep, dom = addr.rpartition("@")
        if not sep or not local:
            continue
        if domain and dom != domain:
            continue
        out.append(local)
    return out


# ---------------------------------------------------------------------------
# The walk (spec §4)
# ---------------------------------------------------------------------------


@dataclass
class PollOutcome:
    messages_seen: int = 0
    processed: int = 0
    quarantined: int = 0
    failed: int = 0
    documents_created: int = 0
    duplicates: int = 0
    replays: int = 0
    attachments_skipped: int = 0


async def _bind_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """``SET LOCAL app.current_tenant`` for the current transaction —
    Postgres only (SQLite has no GUC machinery and no RLS)."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": str(tenant_id)},
        )


def _attachment_disposition(att: MailAttachment) -> str:
    """'INGEST' | 'SKIP_SILENT' | 'SKIP_COUNTED' per spec §4."""
    if att.inline and len(att.data) < INLINE_SKIP_BYTES:
        return "SKIP_SILENT"
    if att.mime not in SUPPORTED_MIME_TYPES:
        return "SKIP_COUNTED"
    if not att.data or len(att.data) > MAX_ATTACHMENT_BYTES:
        return "SKIP_COUNTED"
    return "INGEST"


async def _process_message(
    SessionFactory: async_sessionmaker[AsyncSession],
    msg: MailMessage,
    *,
    mailbox: str,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID | None,
    daily_quota: int,
    outcome: PollOutcome,
) -> str:
    """Process one routed message. Returns 'PROCESSED' | 'QUOTA'.

    Attachments first (committed per attachment — the crash-replay
    contract), ledger row last. Exceptions propagate to the walk, which
    leaves the message in the inbox for the next run.
    """
    async with SessionFactory() as session:
        # Replay/ledger + quota gate.
        async with session.begin():
            await _bind_tenant(session, tenant_id)
            already = (
                await session.execute(
                    select(InboxEmailMessage.id).where(
                        InboxEmailMessage.tenant_id == tenant_id,
                        InboxEmailMessage.mailbox == mailbox,
                        InboxEmailMessage.message_id == msg.message_id,
                    )
                )
            ).scalar_one_or_none()
            if already is not None:
                # Fully processed before — only the folder move failed.
                logger.info(
                    "inbox-poll-mail: message=%s tenant=%s outcome=ALREADY_PROCESSED",
                    msg.message_id,
                    tenant_id,
                )
                return "PROCESSED"
            used_today = await inbox_svc.count_email_documents_today(
                session, tenant_id
            )
        if used_today >= daily_quota:
            logger.warning(
                "inbox-poll-mail: message=%s tenant=%s outcome=QUOTA used=%d quota=%d",
                msg.message_id,
                tenant_id,
                used_today,
                daily_quota,
            )
            return "QUOTA"

        document_count = 0
        skipped_count = 0
        # Attachments first — enumerate over ALL attachments so the
        # per-attachment index (the source_ref suffix) is stable across
        # replays regardless of what qualifies.
        for index, att in enumerate(msg.attachments):
            disposition = _attachment_disposition(att)
            if disposition == "SKIP_SILENT":
                continue
            if disposition == "SKIP_COUNTED":
                skipped_count += 1
                outcome.attachments_skipped += 1
                continue
            source_ref = f"{msg.message_id}#{index}"
            # ``uploaded_blob_id`` is captured as a plain value inside
            # the transaction so a commit failure (e.g. an overlapping
            # run winning the source_ref/sha256 unique) can soft-archive
            # the blob this run just uploaded instead of orphaning it
            # (mirrors the upload-path race backstop in ``ingest``).
            uploaded_blob_id: uuid.UUID | None = None
            try:
                async with session.begin():
                    await _bind_tenant(session, tenant_id)
                    doc, verdict = await inbox_svc.ingest_email_attachment(
                        session,
                        tenant_id,
                        data=att.data,
                        filename=att.filename,
                        mime=att.mime,
                        source_ref=source_ref,
                        company_id=company_id,
                    )
                    if verdict == "INGESTED":
                        uploaded_blob_id = doc.vault_file_id
            except Exception:
                if uploaded_blob_id is not None:
                    with contextlib.suppress(vault_client.VaultError):
                        await vault_client.delete(tenant_id, uploaded_blob_id)
                raise
            document_count += 1
            if verdict == "INGESTED":
                outcome.documents_created += 1
            elif verdict == "DUPLICATE":
                outcome.duplicates += 1
            else:
                outcome.replays += 1
            logger.info(
                "inbox-poll-mail: message=%s tenant=%s attachment=%d doc=%s "
                "outcome=%s size=%d mime=%s",
                msg.message_id,
                tenant_id,
                index,
                doc.id,
                verdict,
                len(att.data),
                att.mime,
            )

        # Ledger row LAST — its presence means every attachment above is
        # durably in. A unique violation here means an overlapping run
        # finished the same message first; that is idempotent success.
        try:
            async with session.begin():
                await _bind_tenant(session, tenant_id)
                session.add(
                    InboxEmailMessage(
                        tenant_id=tenant_id,
                        mailbox=mailbox,
                        message_id=msg.message_id,
                        from_addr=msg.from_addr[:998] if msg.from_addr else None,
                        subject=msg.subject or None,
                        received_at=msg.received_at,
                        processed_at=datetime.now(UTC),
                        document_count=document_count,
                        skipped_count=skipped_count,
                    )
                )
        except IntegrityError:
            logger.info(
                "inbox-poll-mail: message=%s tenant=%s ledger already written "
                "by a concurrent run",
                msg.message_id,
                tenant_id,
            )
        logger.info(
            "inbox-poll-mail: message=%s tenant=%s outcome=PROCESSED "
            "documents=%d skipped=%d",
            msg.message_id,
            tenant_id,
            document_count,
            skipped_count,
        )
        return "PROCESSED"


async def poll_mailbox(
    source: MailSource,
    SessionFactory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
) -> PollOutcome:
    """Drain the catch-all mailbox once (spec §4). Per-message errors
    are logged and leave the message in place for the next cron fire —
    one poisoned message never stops the rest."""
    settings = settings or _default_settings
    outcome = PollOutcome()

    handles = await source.list_messages()
    if not handles:
        return outcome

    async with SessionFactory() as session:
        routes = await load_routing_map(session)

    quarantine = settings.inbox_mail_quarantine_folder
    processed_folder = settings.inbox_mail_processed_folder

    for handle in handles:
        outcome.messages_seen += 1
        try:
            msg = await source.fetch(handle)
        except Exception:
            outcome.failed += 1
            logger.exception("inbox-poll-mail: fetch failed for handle=%s", handle)
            continue

        route: tuple[uuid.UUID, uuid.UUID | None] | None = None
        for token in candidate_tokens(msg.recipients, settings.inbox_mail_domain):
            if token in routes:
                route = routes[token]
                break

        try:
            if route is None:
                # Unroutable — quarantine, NEVER a bounce.
                await source.move(handle, quarantine)
                outcome.quarantined += 1
                logger.warning(
                    "inbox-poll-mail: message=%s outcome=QUARANTINED_NO_ROUTE "
                    "recipients=%d",
                    msg.message_id,
                    len(msg.recipients),
                )
                continue

            tenant_id, company_id = route
            verdict = await _process_message(
                SessionFactory,
                msg,
                mailbox=source.mailbox,
                tenant_id=tenant_id,
                company_id=company_id,
                daily_quota=settings.inbox_email_daily_quota,
                outcome=outcome,
            )
            if verdict == "QUOTA":
                await source.move(handle, quarantine)
                outcome.quarantined += 1
            else:
                await source.move(handle, processed_folder)
                outcome.processed += 1
        except Exception:
            outcome.failed += 1
            logger.exception(
                "inbox-poll-mail: processing failed for message=%s — left in "
                "inbox for the next run",
                getattr(msg, "message_id", handle),
            )

    logger.info(
        "inbox-poll-mail: seen=%d processed=%d quarantined=%d failed=%d "
        "documents=%d duplicates=%d replays=%d skipped=%d",
        outcome.messages_seen,
        outcome.processed,
        outcome.quarantined,
        outcome.failed,
        outcome.documents_created,
        outcome.duplicates,
        outcome.replays,
        outcome.attachments_skipped,
    )
    return outcome
