"""Engine-side contract tests for the comms facades (#32).

Policy (the two-key kill switch, FROM allowlist, draft-vs-send) and transport
(SMTP / Resend / Graph) moved to the app comms module; those tests moved with
them. What remains ENGINE-side and is asserted here:

  (a) each facade POSTs the correct body + headers to /internal/comms/send;
  (b) the module's ``{outcome, provider_id, detail}`` is mapped back onto the
      exact legacy return values (SendResult / EmailResult) + email_send_log
      row the callers depended on;
  (c) a transport failure (connection error / 5xx) raises the right exception
      (EmailError for the email facade, CommsServiceError for customer_email);
  (d) no transport (async-SMTP / Graph / deleted-module) imports linger in the
      engine package.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import text

from saebooks.config import settings
from saebooks.services import email as email_facade
from saebooks.services.comms_client import CommsServiceError


def _comms_url() -> str:
    return f"{settings.comms_service_url.rstrip('/')}/internal/comms/send"


# --------------------------------------------------------------------------- #
# email.py facade — send_raw_email + send_email (no DB needed)                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_raw_email_posts_raw_kind_and_maps_sent(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": "eml_1", "detail": None}
        )
    )
    result = await email_facade.send_raw_email(
        "a@example.com",
        "Hi",
        "<p>x</p>",
        attachments=[email_facade.EmailAttachment("f.pdf", b"%PDF", "application/pdf")],
    )
    assert route.called
    body = json.loads(route.calls[-1].request.content)
    assert body["kind"] == "raw"
    assert body["to"] == ["a@example.com"]
    assert body["subject"] == "Hi"
    assert body["body_html"] == "<p>x</p>"
    assert body["attachments"][0]["filename"] == "f.pdf"
    assert body["attachments"][0]["content_type"] == "application/pdf"
    assert body["attachments"][0]["content_b64"]  # base64 payload present
    # outcome mapping: sent -> mode "smtp", provider_id -> message_id
    assert result.mode == "smtp"
    assert result.message_id == "eml_1"
    assert result.recipients == ("a@example.com",)


@pytest.mark.asyncio
async def test_send_raw_email_billing_receipt_kind(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": "x", "detail": None}
        )
    )
    await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>", kind="billing_receipt")
    body = json.loads(route.calls[-1].request.content)
    assert body["kind"] == "billing_receipt"


@pytest.mark.asyncio
async def test_send_email_template_posts_magic_link_kind(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": None, "detail": None}
        )
    )
    await email_facade.send_email(
        "u@x.com",
        "Login",
        "magic_link_email",
        context={"magic_link": "https://app/x", "expires_minutes": 15},
    )
    body = json.loads(route.calls[-1].request.content)
    assert body["kind"] == "magic_link"
    assert body["body_html"] is None                       # module renders it
    assert body["meta"]["template"] == "magic_link_email"
    assert body["meta"]["context"]["magic_link"] == "https://app/x"


@pytest.mark.asyncio
async def test_token_header_sent_when_configured(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saebooks.config.settings.comms_service_token", "sekret")
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": None, "detail": None}
        )
    )
    await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>")
    assert route.calls[-1].request.headers.get("X-Comms-Token") == "sekret"


@pytest.mark.asyncio
async def test_token_header_absent_when_empty(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saebooks.config.settings.comms_service_token", "")
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": None, "detail": None}
        )
    )
    await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>")
    assert "X-Comms-Token" not in route.calls[-1].request.headers


@pytest.mark.asyncio
async def test_blocked_outcome_maps_to_outbox_mode(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "blocked", "provider_id": None, "detail": "gated"}
        )
    )
    result = await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>")
    assert result.mode == "outbox"


@pytest.mark.asyncio
async def test_connection_error_raises_emailerror(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(_comms_url()).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(email_facade.EmailError):
        await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>")


@pytest.mark.asyncio
async def test_5xx_raises_emailerror(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(_comms_url()).mock(return_value=Response(503, text="down"))
    with pytest.raises(email_facade.EmailError):
        await email_facade.send_raw_email("a@x.com", "s", "<p>h</p>")


# --------------------------------------------------------------------------- #
# customer_email.py facade — needs the DB (audit row + tenant flag read)       #
# --------------------------------------------------------------------------- #


@pytest.fixture
def outbox_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("saebooks.config.settings.mail_outbox_dir", str(tmp_path))


async def _send_customer(**overrides):
    from saebooks.api.v1.auth import DEFAULT_TENANT_ID
    from saebooks.db import AsyncSessionLocal
    from saebooks.services.customer_email import (
        CustomerEmailAttachment,
        send_customer_email,
    )

    async with AsyncSessionLocal() as session:
        # email_send_log is RLS-protected — pin the tenant GUC like the
        # request middleware does.
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
            subject="Tax Invoice INV-1",
            body_html="<p>Please find attached.</p>",
            attachments=[
                CustomerEmailAttachment("INV-1.pdf", b"%PDF-1.5 fake", "application/pdf")
            ],
        )
        kwargs.update(overrides)
        result = await send_customer_email(session, **kwargs)
        await session.commit()
        row = (
            await session.execute(
                text(
                    "SELECT resend_status, resend_message_id, kill_switch_reason, "
                    "resend_error FROM email_send_log WHERE id = :id"
                ),
                {"id": str(result.log_id)},
            )
        ).first()
    return result, row


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_customer_doc_sent_maps_and_logs(
    outbox_tmp: None, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200, json={"outcome": "sent", "provider_id": "re_123", "detail": None}
        )
    )
    result, row = await _send_customer()

    body = json.loads(route.calls[-1].request.content)
    assert body["kind"] == "customer_doc"
    assert body["meta"]["doc_type"] == "invoice"
    assert body["meta"]["from_addr"] == "admin@saee.com.au"
    assert "tenant_outbound_enabled" in body["meta"]      # DB fact forwarded
    assert body["attachments"][0]["filename"] == "INV-1.pdf"

    assert result.mode == "sent"
    assert result.message_id == "re_123"
    assert result.outbox_path is not None
    assert Path(result.outbox_path).exists()              # engine-side .eml audit
    assert row is not None
    assert row[0] == "sent"                               # resend_status
    assert row[1] == "re_123"                             # resend_message_id


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_customer_doc_blocked_maps_and_logs(
    outbox_tmp: None, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200,
            json={"outcome": "blocked", "provider_id": None, "detail": "kill switch off"},
        )
    )
    result, row = await _send_customer()
    assert result.mode == "blocked"
    assert result.reason == "kill switch off"
    assert result.message_id is None
    assert row[0] == "blocked"
    assert row[2] == "kill switch off"                    # kill_switch_reason


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_customer_doc_drafted_maps_and_logs(
    outbox_tmp: None, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_comms_url()).mock(
        return_value=Response(
            200,
            json={
                "outcome": "drafted",
                "provider_id": "AAMkDraftId",
                "detail": "draft mode: saved to Outlook drafts",
            },
        )
    )
    result, row = await _send_customer()
    assert result.mode == "drafted"
    assert result.message_id == "AAMkDraftId"
    assert row[0] == "drafted"
    assert row[1] == "AAMkDraftId"
    assert "draft mode" in (row[2] or "")


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_customer_doc_transport_failure_raises_comms_error(
    outbox_tmp: None, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(_comms_url()).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CommsServiceError):
        await _send_customer()


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_customer_doc_bad_doctype_raises_input_error(outbox_tmp: None) -> None:
    # Validation happens before any POST, so no comms route is needed.
    from saebooks.services.customer_email import CustomerEmailError

    with pytest.raises(CustomerEmailError):
        await _send_customer(doc_type="frobnicate")


# --------------------------------------------------------------------------- #
# (d) no transport / deleted-module imports linger in the engine package       #
# --------------------------------------------------------------------------- #


def test_no_transport_imports_remain_in_engine() -> None:
    """The async-SMTP client, the Graph draft module, and the deleted mailer
    must not be imported anywhere under ``saebooks/``."""
    import saebooks

    root = Path(saebooks.__file__).parent
    # Built by concatenation so the raw tokens never appear as literals.
    forbidden = [
        "aiosmtp" + "lib",
        "smtp" + "lib",
        "services.outlook_drafts",
        "services.mailer",
        "msgraph",
    ]
    import_re = {
        tok: re.compile(rf"^\s*(?:import|from)\s+.*{re.escape(tok)}", re.MULTILINE)
        for tok in forbidden
    }
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for tok, rx in import_re.items():
            if rx.search(src):
                offenders.append(f"{path}: imports {tok}")
    assert not offenders, "transport imports still present:\n" + "\n".join(offenders)
