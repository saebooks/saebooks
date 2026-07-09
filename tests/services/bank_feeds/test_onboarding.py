"""Tests for the bank_feeds onboarding / sync / offboard orchestration layer.

Most of the SissClient behaviour is already covered in test_client /
test_endpoints / test_repo. These tests focus on the *glue* module that
the router layer calls — consent initiation shaping, callback upsert,
per-account revoke, and the sync loop.

Strategy: monkey-patch the two SISS-touching functions
(`initiate_consumer_consent`, `list_accounts`, `revoke_account`,
`iter_transactions`, `delete_client`) so the tests don't spin up an
httpx + respx stack — the repo/db integration is the interesting bit
here.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.company import Company
from saebooks.services.bank_feeds import endpoints, onboarding

pytestmark = pytest.mark.postgres_only


def _test_settings(**overrides: Any) -> Settings:
    """Build a Settings with SISS creds populated so siss_configured is true."""
    base = dict(
        SISS_CLIENT_ID="test-client",
        SISS_CLIENT_SECRET="test-secret",
        SISS_SUBSCRIPTION_KEY="test-key",
        SISS_TOKEN_URL="https://auth.example/oauth/token",
        SISS_API_BASE="https://api.example/cdr-au/v1/",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _first_company_and_bank_account() -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "1-1110",
                )
            )
        ).scalars().first()
        assert bank is not None
        return company.id, bank.id


# ---------------------------------------------------------------------- #
# siss_configured + not-configured error                                 #
# ---------------------------------------------------------------------- #


def test_siss_configured_false_with_blank_creds() -> None:
    """Empty creds → siss_configured returns False (no crash)."""
    s = Settings(SISS_CLIENT_ID="", SISS_CLIENT_SECRET="", SISS_SUBSCRIPTION_KEY="")  # type: ignore[arg-type]
    assert onboarding.siss_configured(s) is False


def test_siss_configured_true_with_populated_creds() -> None:
    s = _test_settings()
    assert onboarding.siss_configured(s) is True


async def test_siss_client_raises_when_unconfigured() -> None:
    s = Settings(SISS_CLIENT_ID="", SISS_CLIENT_SECRET="", SISS_SUBSCRIPTION_KEY="")  # type: ignore[arg-type]
    with pytest.raises(onboarding.SissNotConfiguredError):
        async with onboarding.siss_client(s):
            pass  # pragma: no cover — raises before yield


# ---------------------------------------------------------------------- #
# initiate_consent                                                       #
# ---------------------------------------------------------------------- #


async def test_initiate_consent_extracts_redirect_and_consent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_consumer(client: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "data": {
                "redirectUrl": "https://auth.sissdata/consent/abc",
                "consentId": "consent-guid-123",
            }
        }

    monkeypatch.setattr(endpoints, "initiate_consumer_consent", fake_consumer)

    result = await onboarding.initiate_consent(
        settings=_test_settings(),
        institution_id="CBA",
        redirect_uri="https://host/callback",
        variant="consumer",
    )
    assert result.redirect_url == "https://auth.sissdata/consent/abc"
    assert result.consent_id == "consent-guid-123"
    assert captured["institution_id"] == "CBA"
    assert captured["redirect_uri"] == "https://host/callback"


async def test_initiate_consent_caf_variant_calls_authorise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, int] = {"consumer": 0, "caf": 0}

    async def fake_caf(client: Any, **kwargs: Any) -> dict[str, Any]:
        calls["caf"] += 1
        return {
            "data": {
                "redirectUrl": "https://auth.sissdata/caf/abc",
                "consentId": "caf-123",
            }
        }

    async def fake_consumer(client: Any, **kwargs: Any) -> dict[str, Any]:
        calls["consumer"] += 1
        return {"data": {"redirectUrl": "x", "consentId": "y"}}

    monkeypatch.setattr(endpoints, "initiate_caf_consent", fake_caf)
    monkeypatch.setattr(endpoints, "initiate_consumer_consent", fake_consumer)

    await onboarding.initiate_consent(
        settings=_test_settings(),
        institution_id="XYZ",
        redirect_uri="https://host/callback",
        variant="caf",
    )
    assert calls["caf"] == 1
    assert calls["consumer"] == 0


# ---------------------------------------------------------------------- #
# resolve_callback — upserts client + discovered accounts                #
# ---------------------------------------------------------------------- #


async def test_resolve_callback_upserts_client_and_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": f"ACCT-{uuid.uuid4()}",
                        "maskedNumber": "062-xxx-1234",
                        "displayName": "CBA Smart Access",
                        "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS",
                        "sds": {
                            "sdsInstitutionId": "CBA",
                            "feedType": "CDR",
                            "processingStatus": "OK",
                            "lastTransactionPostedId": "T-0",
                        },
                    }
                ]
            }
        }

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)

    sds_client = f"SDS-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        result = await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        await session.commit()

    assert result.bank_feed_client.sds_client_id == sds_client
    assert len(result.discovered_accounts) == 1
    assert result.discovered_accounts[0].display_name == "CBA Smart Access"
    assert result.discovered_accounts[0].sds_institution_id == "CBA"

    # Cleanup so later tests start fresh
    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


# ---------------------------------------------------------------------- #
# link_account_to_ledger                                                 #
# ---------------------------------------------------------------------- #


async def test_link_account_to_ledger_updates_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": f"ACCT-LINK-{uuid.uuid4().hex[:6]}",
                        "displayName": "For link test",
                        "sds": {"sdsInstitutionId": "TEST"},
                    }
                ]
            }
        }

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)

    sds_client = f"SDS-LINK-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as session:
        result = await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        # Pick a different ledger account and relink
        other = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "1-1180",
                )
            )
        ).scalars().first()
        assert other is not None
        feed = result.discovered_accounts[0]
        updated = await onboarding.link_account_to_ledger(
            session,
            bank_feed_account_id=feed.id,
            ledger_account_id=other.id,
        )
        assert updated.ledger_account_id == other.id
        await session.commit()

    # Cleanup
    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


# ---------------------------------------------------------------------- #
# revoke_feed_account                                                    #
# ---------------------------------------------------------------------- #


async def test_revoke_feed_account_calls_upstream_and_stamps_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    sds_client = f"SDS-REV-{uuid.uuid4().hex[:6]}"
    acct_id = f"ACCT-REV-{uuid.uuid4().hex[:6]}"

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": acct_id,
                        "displayName": "To revoke",
                        "sds": {"sdsInstitutionId": "TEST"},
                    }
                ]
            }
        }

    revoke_calls: list[str] = []

    async def fake_revoke(client: Any, *, account_id: str) -> None:
        revoke_calls.append(account_id)

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)
    monkeypatch.setattr(endpoints, "revoke_account", fake_revoke)

    async with AsyncSessionLocal() as session:
        result = await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        feed = result.discovered_accounts[0]
        revoked = await onboarding.revoke_feed_account(
            session,
            bank_feed_account_id=feed.id,
            settings=_test_settings(),
        )
        assert revoked.revoked_at is not None
        await session.commit()

    assert revoke_calls == [acct_id]

    # Cleanup
    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


async def test_revoke_feed_account_local_only_skips_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    sds_client = f"SDS-LOC-{uuid.uuid4().hex[:6]}"

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": f"ACCT-LOC-{uuid.uuid4().hex[:6]}",
                        "displayName": "Local only",
                        "sds": {"sdsInstitutionId": "TEST"},
                    }
                ]
            }
        }

    revoke_calls: list[str] = []

    async def fake_revoke(client: Any, *, account_id: str) -> None:
        revoke_calls.append(account_id)

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)
    monkeypatch.setattr(endpoints, "revoke_account", fake_revoke)

    async with AsyncSessionLocal() as session:
        result = await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        feed = result.discovered_accounts[0]
        await onboarding.revoke_feed_account(
            session,
            bank_feed_account_id=feed.id,
            settings=_test_settings(),
            skip_upstream=True,
        )
        await session.commit()

    assert revoke_calls == []

    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


# ---------------------------------------------------------------------- #
# sync_account — pulls txns, dedupes, advances cursor                    #
# ---------------------------------------------------------------------- #


async def test_sync_account_inserts_lines_and_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    sds_client = f"SDS-SYNC-{uuid.uuid4().hex[:6]}"
    acct_id = f"ACCT-SYNC-{uuid.uuid4().hex[:6]}"

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": acct_id,
                        "displayName": "Sync me",
                        "sds": {"sdsInstitutionId": "TEST"},
                    }
                ]
            }
        }

    async def fake_iter(client: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for i in range(3):
            yield {
                "transactionId": f"T-{i+1}",
                "accountId": acct_id,
                "postingDateTime": "2026-05-01T10:00:00Z",
                "amount": f"{100 + i}.00",
                "description": f"Txn {i}",
            }

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)
    monkeypatch.setattr(endpoints, "iter_transactions", fake_iter)

    async with AsyncSessionLocal() as session:
        result = await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        feed = result.discovered_accounts[0]
        outcome = await onboarding.sync_account(
            session,
            bank_feed_account_id=feed.id,
            settings=_test_settings(),
        )
        await session.commit()

    assert outcome.transactions_seen == 3
    assert outcome.lines_inserted == 3
    assert outcome.cursor_advanced_to == "T-3"

    async with AsyncSessionLocal() as session:
        refetched = await session.get(BankFeedAccount, feed.id)
        assert refetched is not None
        assert refetched.last_transaction_posted_id == "T-3"
        # Re-sync with no new txns is a no-op (dedup)

    async def fake_iter_none(
        client: Any, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    monkeypatch.setattr(endpoints, "iter_transactions", fake_iter_none)
    async with AsyncSessionLocal() as session:
        outcome2 = await onboarding.sync_account(
            session,
            bank_feed_account_id=feed.id,
            settings=_test_settings(),
        )
        await session.commit()
    assert outcome2.lines_inserted == 0

    # Cleanup
    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()


async def test_sync_account_skips_revoked() -> None:
    company_id, ledger_id = await _first_company_and_bank_account()
    from datetime import datetime

    sds_client = f"SDS-SKIP-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as session:
        # Manually create client + revoked account
        bfc = BankFeedClient(company_id=company_id, sds_client_id=sds_client)
        session.add(bfc)
        await session.flush()
        acct = BankFeedAccount(
            company_id=company_id,
            bank_feed_client_id=bfc.id,
            ledger_account_id=ledger_id,
            sds_account_id=f"ACCT-SKIP-{uuid.uuid4().hex[:6]}",
            sds_institution_id="TEST",
            revoked_at=datetime.now(),
        )
        session.add(acct)
        await session.commit()

        outcome = await onboarding.sync_account(
            session,
            bank_feed_account_id=acct.id,
            settings=_test_settings(),
        )
        assert outcome.transactions_seen == 0
        assert outcome.lines_inserted == 0

        await session.delete(acct)
        await session.delete(bfc)
        await session.commit()


# ---------------------------------------------------------------------- #
# offboard_company                                                       #
# ---------------------------------------------------------------------- #


async def test_offboard_company_soft_revoke_local_accounts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    company_id, ledger_id = await _first_company_and_bank_account()

    sds_client = f"SDS-OFF-{uuid.uuid4().hex[:6]}"

    async def fake_list_accounts(client: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "data": {
                "accounts": [
                    {
                        "accountId": f"ACCT-OFF-{uuid.uuid4().hex[:6]}",
                        "displayName": "To offboard",
                        "sds": {"sdsInstitutionId": "TEST"},
                    }
                ]
            }
        }

    delete_calls: list[str] = []

    async def fake_delete_client(client: Any, *, sds_client_id: str) -> None:
        delete_calls.append(sds_client_id)

    monkeypatch.setattr(endpoints, "list_accounts", fake_list_accounts)
    monkeypatch.setattr(endpoints, "delete_client", fake_delete_client)

    async with AsyncSessionLocal() as session:
        await onboarding.resolve_callback(
            session,
            company_id=company_id,
            sds_client_id=sds_client,
            default_ledger_account_id=ledger_id,
            settings=_test_settings(),
        )
        # Soft revoke only — skip_upstream=True
        result = await onboarding.offboard_company(
            session,
            company_id=company_id,
            export_dir=str(tmp_path),
            settings=_test_settings(),
            skip_upstream=True,
        )
        await session.commit()

    assert result.client_deleted_upstream is False
    assert result.accounts_revoked_locally == 1
    assert result.export_path is not None
    import os
    assert os.path.exists(result.export_path)
    assert delete_calls == []

    # Now hard-delete upstream
    async with AsyncSessionLocal() as session:
        result2 = await onboarding.offboard_company(
            session,
            company_id=company_id,
            export_dir=str(tmp_path),
            settings=_test_settings(),
            skip_upstream=False,
        )
        await session.commit()
    assert result2.client_deleted_upstream is True
    assert delete_calls == [sds_client]

    # Cleanup
    async with AsyncSessionLocal() as session:
        bfc = (
            await session.execute(
                select(BankFeedClient).where(
                    BankFeedClient.sds_client_id == sds_client
                )
            )
        ).scalar_one()
        await session.delete(bfc)
        await session.commit()
