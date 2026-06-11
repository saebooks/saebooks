"""Draft mode for send_customer_email — Outlook drafts via mocked Graph.

When SAEBOOKS_EMAIL_DRAFT_MODE is on, send_customer_email must:
* create a Graph draft (mocked here with respx) and return mode='drafted'
* record resend_status='drafted' in email_send_log with the Graph draft id
* never consult the Resend path (no Resend route is mocked — any call
  through would error the test)
* fail closed (mode='failed', status logged) when Graph config is absent

The two-key kill switch path is untouched and covered by existing tests.
"""
from __future__ import annotations

import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import text

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.services.customer_email import (
    CustomerEmailAttachment,
    send_customer_email,
)

pytestmark = pytest.mark.postgres_only

_GRAPH_TENANT = "11111111-2222-3333-4444-555555555555"
_MAILBOX = "drafts-test@example.com"
_TOKEN_URL = (
    f"https://login.microsoftonline.com/{_GRAPH_TENANT}/oauth2/v2.0/token"
)
_CREATE_URL = f"https://graph.microsoft.com/v1.0/users/{_MAILBOX}/messages"


def _settings_objects() -> list:
    """Every module-level ``settings`` reference the code under test reads.

    Other tests in the full suite re-instantiate/rebind
    ``saebooks.config.settings``, so the object this test module captured
    at collection time, the one customer_email bound at import time, and
    the one outlook_drafts binds at (lazy) import time may differ. Patch
    them all — identity-deduped — or the patch silently misses in CI.
    """
    import saebooks.config as cfg
    import saebooks.services.customer_email as ce
    import saebooks.services.outlook_drafts as od

    return list({id(o): o for o in (settings, cfg.settings, ce.settings, od.settings)}.values())


def _patch_all(monkeypatch: pytest.MonkeyPatch, key: str, value) -> None:
    for obj in _settings_objects():
        monkeypatch.setattr(obj, key, value)


@pytest.fixture
def draft_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_all(monkeypatch, "customer_email_draft_mode", True)
    _patch_all(monkeypatch, "graph_tenant_id", _GRAPH_TENANT)
    _patch_all(monkeypatch, "graph_client_id", "client-id")
    _patch_all(monkeypatch, "graph_client_secret", "client-secret")
    _patch_all(monkeypatch, "graph_draft_mailbox", _MAILBOX)
    _patch_all(monkeypatch, "mail_outbox_dir", str(tmp_path))
    # Reset the module-level Graph token cache between tests.
    import saebooks.services.outlook_drafts as od

    od._token_cache = None


async def _call_send(**overrides):
    async with AsyncSessionLocal() as session:
        # email_send_log is RLS-protected — pin the tenant GUC for this
        # session the same way the request middleware does.
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, false)"),
            {"tid": str(DEFAULT_TENANT_ID)},
        )
        kwargs = dict(
            tenant_id=DEFAULT_TENANT_ID,
            doc_type="invoice",
            doc_id=uuid.uuid4(),
            doc_version=1,
            sent_by_user_id=None,
            from_addr="admin@saee.com.au",
            to=["customer@example.com"],
            subject="Tax Invoice INV-042",
            body_html="<p>Please find attached.</p>",
            attachments=[
                CustomerEmailAttachment(
                    filename="INV-042.pdf", content=b"%PDF-1.5 fake"
                )
            ],
        )
        kwargs.update(overrides)
        result = await send_customer_email(session, **kwargs)
        await session.commit()

        row = (
            await session.execute(
                text(
                    "SELECT resend_status, resend_message_id, kill_switch_reason "
                    "FROM email_send_log WHERE id = :id"
                ),
                {"id": str(result.log_id)},
            )
        ).first()
    return result, row


@pytest.mark.asyncio
async def test_draft_mode_creates_outlook_draft(
    draft_mode: None, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_TOKEN_URL).mock(
        return_value=Response(
            200, json={"access_token": "tok", "expires_in": 3600}
        )
    )
    respx_mock.post(_CREATE_URL).mock(
        return_value=Response(
            201,
            json={"id": "AAMkADraftId", "webLink": "https://outlook/x"},
        )
    )

    result, row = await _call_send()

    assert result.mode == "drafted"
    assert result.message_id == "AAMkADraftId"
    assert row is not None
    assert row[0] == "drafted"
    assert row[1] == "AAMkADraftId"
    assert "draft mode" in (row[2] or "")

    # The draft payload carried the attachment.
    create_call = respx_mock.calls[-1]
    import json as _json

    payload = _json.loads(create_call.request.content)
    assert payload["subject"] == "Tax Invoice INV-042"
    assert payload["attachments"][0]["name"] == "INV-042.pdf"
    assert payload["toRecipients"][0]["emailAddress"]["address"] == (
        "customer@example.com"
    )


@pytest.mark.asyncio
async def test_draft_mode_fails_closed_without_graph_config(
    draft_mode: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    _patch_all(monkeypatch, "graph_client_secret", "")

    result, row = await _call_send()

    assert result.mode == "failed"
    assert result.message_id is None
    assert row is not None
    assert row[0] == "failed"
    assert "not configured" in (row[2] or "")
