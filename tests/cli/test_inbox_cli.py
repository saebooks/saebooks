"""Wiring tests for the ``inbox-poll-mail`` / ``inbox-extract`` CLI
commands (Document Inbox phase 3).

Same posture as tests/test_cli.py: argparse plumbing + happy paths with
the heavy lifting monkeypatched. The walk itself is covered in
tests/services/test_inbox_mail.py, the sweep machinery in
tests/services/test_inbox_sweep.py, and the strict-role RLS contract by
the SECDEF probes in tests/test_rls_inbox_email.py.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from saebooks import cli
from saebooks.config import settings as _settings
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import inbox_mail


@pytest.fixture(autouse=True)
def _business_edition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "edition", "business")


# ---------------------------------------------------------------------------
# inbox-poll-mail
# ---------------------------------------------------------------------------


def test_poll_mail_unconfigured_provider_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_provider", "")
    rc = cli.main(["inbox-poll-mail", "--allow-bypass"])
    assert rc == 2


def test_poll_mail_below_business_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_settings, "edition", "offline")
    rc = cli.main(["inbox-poll-mail", "--allow-bypass"])
    assert rc == 2


def test_poll_mail_runs_walk_and_closes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSource:
        mailbox = "catchall@in.test"

        async def close(self) -> None:
            events.append("closed")

    async def fake_poll(source: Any, factory: Any, *, settings: Any) -> Any:
        events.append("polled")
        return inbox_mail.PollOutcome(messages_seen=2, processed=2)

    monkeypatch.setattr(
        inbox_mail, "mail_source_from_settings", lambda s: FakeSource()
    )
    monkeypatch.setattr(inbox_mail, "poll_mailbox", fake_poll)

    rc = cli.main(["inbox-poll-mail", "--allow-bypass"])
    assert rc == 0
    assert events == ["polled", "closed"]


def test_poll_mail_failures_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSource:
        mailbox = "catchall@in.test"

        async def close(self) -> None:
            pass

    async def fake_poll(source: Any, factory: Any, *, settings: Any) -> Any:
        return inbox_mail.PollOutcome(messages_seen=1, failed=1)

    monkeypatch.setattr(
        inbox_mail, "mail_source_from_settings", lambda s: FakeSource()
    )
    monkeypatch.setattr(inbox_mail, "poll_mailbox", fake_poll)
    rc = cli.main(["inbox-poll-mail", "--allow-bypass"])
    assert rc == 1


# ---------------------------------------------------------------------------
# inbox-extract
# ---------------------------------------------------------------------------


def _patch_sweep_tenants(
    monkeypatch: pytest.MonkeyPatch, tenants: list[uuid.UUID]
) -> None:
    async def fake_enum(session: Any) -> list[uuid.UUID]:
        return tenants

    monkeypatch.setattr(cli, "_enumerate_sweep_tenants", fake_enum)


def test_inbox_extract_nothing_to_sweep_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sweep_tenants(monkeypatch, [])
    rc = cli.main(["inbox-extract", "--allow-bypass"])
    assert rc == 0


def test_inbox_extract_community_edition_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_settings, "edition", "community")
    rc = cli.main(["inbox-extract", "--allow-bypass"])
    assert rc == 2


def test_inbox_extract_walks_reclaim_claim_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = uuid.uuid4()
    doc_ids = [uuid.uuid4(), uuid.uuid4()]
    calls: dict[str, Any] = {"reclaim": 0, "claim": [], "process": []}

    _patch_sweep_tenants(monkeypatch, [tid])

    async def fake_reclaim(session: Any, tenant_id: Any, **kw: Any) -> int:
        assert tenant_id == tid
        calls["reclaim"] += 1
        return 1

    async def fake_claim(session: Any, tenant_id: Any, *, batch: int) -> list:
        calls["claim"].append(batch)
        return doc_ids

    async def fake_process(
        session: Any, tenant_id: Any, doc_id: Any, *, extract_enabled: bool
    ) -> Any:
        calls["process"].append((doc_id, extract_enabled))
        return object()

    monkeypatch.setattr(inbox_svc, "sweep_reclaim", fake_reclaim)
    monkeypatch.setattr(inbox_svc, "sweep_claim", fake_claim)
    monkeypatch.setattr(inbox_svc, "sweep_process_claimed", fake_process)

    rc = cli.main(["inbox-extract", "--allow-bypass", "--batch", "7"])
    assert rc == 0
    assert calls["reclaim"] == 1
    assert calls["claim"] == [7]
    assert [d for d, _ in calls["process"]] == doc_ids
    # Business edition → the model is consulted.
    assert all(enabled for _, enabled in calls["process"])


def test_inbox_extract_tenant_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sweep_tenants(monkeypatch, [uuid.uuid4()])

    async def boom(session: Any, tenant_id: Any, **kw: Any) -> int:
        raise RuntimeError("simulated tenant failure")

    monkeypatch.setattr(inbox_svc, "sweep_reclaim", boom)
    rc = cli.main(["inbox-extract", "--allow-bypass"])
    assert rc == 1
