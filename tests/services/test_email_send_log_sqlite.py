"""Regression: email_send_log audit rows and change_log writes work on SQLite.

* ``email_send_log`` was created only by Postgres migrations, so
  bootstrap_schema never built it on the SQLite/Community backend and every
  send failed to write its audit row. The ORM model now creates it, and the
  write path uses the ORM (array columns carry a JSON variant) so lists bind on
  SQLite.
* ``change_log.id`` was a ``BIGINT PRIMARY KEY`` that does not autoincrement on
  SQLite, so every write (change_log append) failed with a NOT NULL violation.
  The Integer variant emits ``INTEGER PRIMARY KEY`` on SQLite.

Both run on Postgres too (the constructs are cross-dialect), so no marker.
"""
from __future__ import annotations

import uuid

from saebooks.db import AsyncSessionLocal
from saebooks.models.email_send_log import EmailSendLog
from saebooks.services import change_log as change_log_svc
from saebooks.services.customer_email import _record_send_log

_T = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_change_log_append_autoincrements() -> None:
    async with AsyncSessionLocal() as s:
        r1 = await change_log_svc.append(
            s, entity="widget", entity_id=uuid.uuid4(), op="create",
            actor="t", payload={"a": 1}, version=1,
        )
        r2 = await change_log_svc.append(
            s, entity="widget", entity_id=uuid.uuid4(), op="create",
            actor="t", payload={"a": 2}, version=1,
        )
        await s.commit()
        assert r1.id is not None and r2.id is not None
        assert r2.id > r1.id


async def test_email_send_log_audit_row_writes_and_round_trips() -> None:
    async with AsyncSessionLocal() as s:
        rid = await _record_send_log(
            s, tenant_id=_T, doc_type="invoice", doc_id=uuid.uuid4(),
            doc_version=1, sent_by_user_id=None, from_addr="a@example.com",
            to=["b@example.com"], cc=["c@example.com"], bcc=[], subject="S",
            body_html="<p>hi</p>", body_text=None, attachment_filenames=[],
            attachment_bytes=[], attachment_sha256=[], attachment_content_types=[],
            resend_message_id=None, resend_status="blocked", resend_error=None,
            kill_switch_reason="env off",
        )
        await s.commit()
        row = await s.get(EmailSendLog, rid)
        assert row is not None
        assert row.to_addrs == ["b@example.com"]
        assert row.cc_addrs == ["c@example.com"]
        assert row.resend_status == "blocked"


async def test_email_send_log_attachment_bytes_round_trip() -> None:
    # Real (non-empty) attachment payloads: raw bytes are not JSON-serialisable,
    # so the SQLite variant must base64 them on bind and decode on load. The
    # empty-list case above cannot catch this.
    pdf = b"%PDF-1.5 fake body \x00\xff binary"
    async with AsyncSessionLocal() as s:
        rid = await _record_send_log(
            s, tenant_id=_T, doc_type="invoice", doc_id=uuid.uuid4(),
            doc_version=1, sent_by_user_id=None, from_addr="a@example.com",
            to=["b@example.com"], cc=[], bcc=[], subject="S",
            body_html="<p>hi</p>", body_text=None,
            attachment_filenames=["INV-1.pdf"],
            attachment_bytes=[pdf],
            attachment_sha256=["deadbeef"],
            attachment_content_types=["application/pdf"],
            resend_message_id=None, resend_status="drafted", resend_error=None,
            kill_switch_reason=None,
        )
        await s.commit()
        s.expire_all()
        row = await s.get(EmailSendLog, rid)
        assert row is not None
        assert row.attachment_filenames == ["INV-1.pdf"]
        assert row.attachment_bytes == [pdf]
        assert row.attachment_content_types == ["application/pdf"]
