"""High-level bank-feeds onboarding + sync orchestrators.

This module sits *above* ``client.py`` / ``endpoints.py`` / ``repo.py``
and is the glue the router layer calls. It keeps the router thin by
centralising:

- How we build a configured ``SissClient`` from ``Settings`` (one place
  that knows about env-var plumbing).
- The initiate-consent → callback → upsert flow.
- The per-account revoke flow (DELETE upstream + mark revoked locally).
- The whole-client delete flow (Batch K offboarding).
- The transaction-sync loop used by both the "Sync now" button and the
  daily CLI scheduler.

The router layer doesn't import anything from ``endpoints`` / ``repo`` /
``client`` directly — everything it needs is re-exposed here.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.config import settings as default_settings
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.services.bank_feeds import endpoints, repo
from saebooks.services.bank_feeds.client import SissClient
from saebooks.services.bank_feeds.errors import SissError
from saebooks.services.bank_feeds.token import TokenCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# Client construction                                                    #
# ---------------------------------------------------------------------- #


class SissNotConfiguredError(RuntimeError):
    """Raised when the SISS env vars are empty and we tried to use SISS.

    The router layer catches this and renders a "not configured" banner
    on the admin page, rather than a 500. Helps keep the Community/no-
    creds install look sane.
    """


def siss_configured(settings: Settings | None = None) -> bool:
    """Cheap boolean: do we have enough env to talk to SISS at all?"""
    s = settings or default_settings
    return bool(s.siss_client_id and s.siss_client_secret and s.siss_subscription_key)


@dataclass(frozen=True)
class ResolvedSissCreds:
    """Effective SISS creds for a context, plus where they came from.

    ``source`` is ``"company"`` when Batch-II per-company creds applied,
    ``"env"`` otherwise. UI layers surface this on the credentials page
    so an admin can see at a glance which set the router is using.
    """

    client_id: str
    client_secret: str
    subscription_key: str
    token_url: str
    api_base: str
    environment: str
    source: str  # "company" | "env"


async def resolve_company_siss_creds(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> ResolvedSissCreds:
    """Pick per-company creds when FLAG_PER_COMPANY_SISS + company row both have them,
    otherwise fall back to env-var creds. Raises ``SissNotConfiguredError``
    when neither source yields a complete set.

    Importing ``features`` and ``crypto`` inside the function dodges
    the import cycle — ``features`` imports ``config`` which imports
    nothing from this module, but keeping the lazy import pins the
    dependency direction.
    """
    from saebooks.models.company import Company
    from saebooks.services import crypto as crypto_svc
    from saebooks.services import features as features_svc

    s = settings or default_settings
    per_company_flag_on = features_svc.is_enabled(
        features_svc.FLAG_PER_COMPANY_SISS, settings=s
    )

    if per_company_flag_on:
        company = await session.get(Company, company_id)
        if company is not None and company.siss_client_id:
            # Refuse to use per-company creds if the encryption key is
            # absent — decrypting a ciphertext without a key is meaningless,
            # and silently falling through to env would be surprising.
            try:
                client_secret = crypto_svc.decrypt_field(
                    company.siss_client_secret_encrypted or "", settings=s
                )
                subscription_key = crypto_svc.decrypt_field(
                    company.siss_subscription_key_encrypted or "", settings=s
                )
            except crypto_svc.FieldEncryptionError as exc:
                raise SissNotConfiguredError(
                    f"Per-company SISS creds present but undecryptable: {exc}"
                ) from exc
            if client_secret and subscription_key:
                # Environment picker — sandbox flips the api_base only
                # when SISS eventually ships a distinct sandbox host.
                # Today both environments share the env-configured URLs.
                env = (company.siss_environment or "production").lower()
                return ResolvedSissCreds(
                    client_id=company.siss_client_id,
                    client_secret=client_secret,
                    subscription_key=subscription_key,
                    token_url=s.siss_token_url,
                    api_base=s.siss_api_base,
                    environment=env,
                    source="company",
                )

    # Env fall-through.
    if not siss_configured(s):
        raise SissNotConfiguredError(
            "SISS not configured — set SISS_CLIENT_ID, SISS_CLIENT_SECRET "
            "and SISS_SUBSCRIPTION_KEY via env or .env, or configure "
            "per-company credentials under /admin/bank-feeds/credentials."
        )
    return ResolvedSissCreds(
        client_id=s.siss_client_id,
        client_secret=s.siss_client_secret,
        subscription_key=s.siss_subscription_key,
        token_url=s.siss_token_url,
        api_base=s.siss_api_base,
        environment="sandbox" if s.siss_sandbox else "production",
        source="env",
    )


def _client_from_creds(creds: ResolvedSissCreds) -> SissClient:
    cache = TokenCache(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        token_url=creds.token_url,
    )
    return SissClient(
        api_base=creds.api_base,
        subscription_key=creds.subscription_key,
        token_cache=cache,
    )


@asynccontextmanager
async def siss_client(settings: Settings | None = None) -> AsyncIterator[SissClient]:
    """Build a configured ``SissClient`` from ``Settings`` and clean it up.

    Raises ``SissNotConfiguredError`` if the three env creds aren't all
    set — callers should catch this and surface "not configured" in the
    UI rather than letting it bubble as a 500.
    """
    s = settings or default_settings
    if not siss_configured(s):
        raise SissNotConfiguredError(
            "SISS not configured — set SISS_CLIENT_ID, SISS_CLIENT_SECRET "
            "and SISS_SUBSCRIPTION_KEY via env or .env."
        )
    cache = TokenCache(
        client_id=s.siss_client_id,
        client_secret=s.siss_client_secret,
        token_url=s.siss_token_url,
    )
    client = SissClient(
        api_base=s.siss_api_base,
        subscription_key=s.siss_subscription_key,
        token_cache=cache,
    )
    async with client:
        yield client


@asynccontextmanager
async def siss_client_for_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[SissClient]:
    """``siss_client``'s per-company cousin (Batch II).

    When ``FLAG_PER_COMPANY_SISS`` is on and the company has stored creds
    the returned client is driven by those; otherwise falls back to env.
    Callers that only need per-company creds in one place can use this
    directly; the rest of the codebase is gradually migrated.
    """
    creds = await resolve_company_siss_creds(session, company_id, settings=settings)
    client = _client_from_creds(creds)
    async with client:
        yield client


# ---------------------------------------------------------------------- #
# Onboarding — consent initiation + callback                             #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConsentInitiation:
    """What the UI needs to redirect the end user to SISS."""

    redirect_url: str       # Hosted consent URL the user's browser goes to
    consent_id: str         # Upstream consent guid (for support correlation)


async def initiate_consent(
    *,
    settings: Settings | None = None,
    institution_id: str,
    redirect_uri: str,
    variant: str = "consumer",
    scopes: list[str] | None = None,
    existing_sds_client_id: str | None = None,
) -> ConsentInitiation:
    """Call SISS to start a consent flow; return where to send the user.

    ``variant`` is ``"consumer"`` (MyData / CDR OAuth) or ``"caf"`` (the
    PDF fallback used by institutions without CDR). Default ``consumer``
    matches plan Q2.
    """
    async with siss_client(settings) as client:
        if variant == "caf":
            envelope = await endpoints.initiate_caf_consent(
                client,
                institution_id=institution_id,
                redirect_uri=redirect_uri,
                scopes=scopes,
                sds_client_id=existing_sds_client_id,
            )
        else:
            envelope = await endpoints.initiate_consumer_consent(
                client,
                institution_id=institution_id,
                redirect_uri=redirect_uri,
                scopes=scopes,
                sds_client_id=existing_sds_client_id,
            )
    data = envelope.get("data") or {}
    redirect_url = data.get("redirectUrl") or data.get("redirect_url")
    consent_id = data.get("consentId") or data.get("consent_id")
    if not redirect_url or not consent_id:
        raise SissError(
            f"SISS consent response missing redirectUrl/consentId: {envelope!r}",
            http_status=0,
        )
    return ConsentInitiation(
        redirect_url=str(redirect_url),
        consent_id=str(consent_id),
    )


@dataclass(frozen=True)
class CallbackResult:
    """Outcome of resolving a completed consent flow."""

    bank_feed_client: BankFeedClient
    discovered_accounts: list[BankFeedAccount]


async def resolve_callback(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    sds_client_id: str,
    default_ledger_account_id: uuid.UUID,
    settings: Settings | None = None,
) -> CallbackResult:
    """Record the upstream client + pull-and-upsert its accounts.

    Called from the ``/callback`` route once SISS has redirected the
    user back with an ``sdsClientId`` in the query string. All
    discovered accounts are upserted pinned to ``default_ledger_account_id``
    — the user then maps them to the correct CoA accounts via the
    ``POST /admin/bank-feeds/link`` form (plan Q4: always manual on
    first link, store mapping for resync).
    """
    bfc = await repo.get_or_create_client(
        session,
        company_id=company_id,
        sds_client_id=sds_client_id,
    )
    accounts: list[BankFeedAccount] = []
    async with siss_client(settings) as client:
        envelope = await endpoints.list_accounts(
            client, sds_client_id=sds_client_id, page_size=100
        )
    data = envelope.get("data") or {}
    raw_accounts = data.get("accounts") or []
    for raw in raw_accounts:
        acct = await repo.upsert_bank_feed_account(
            session,
            company_id=company_id,
            bank_feed_client_id=bfc.id,
            ledger_account_id=default_ledger_account_id,
            account=raw,
        )
        accounts.append(acct)
    return CallbackResult(bank_feed_client=bfc, discovered_accounts=accounts)


# ---------------------------------------------------------------------- #
# Linking — persist the bank-account -> CoA mapping                      #
# ---------------------------------------------------------------------- #


async def link_account_to_ledger(
    session: AsyncSession,
    *,
    bank_feed_account_id: uuid.UUID,
    ledger_account_id: uuid.UUID,
) -> BankFeedAccount:
    """Set the chart-of-accounts account that feed lines post to."""
    row = await session.get(BankFeedAccount, bank_feed_account_id)
    if row is None:
        raise ValueError(f"BankFeedAccount {bank_feed_account_id} not found")
    row.ledger_account_id = ledger_account_id
    await session.flush()
    return row


# ---------------------------------------------------------------------- #
# Per-account revoke                                                     #
# ---------------------------------------------------------------------- #


async def revoke_feed_account(
    session: AsyncSession,
    *,
    bank_feed_account_id: uuid.UUID,
    settings: Settings | None = None,
    skip_upstream: bool = False,
) -> BankFeedAccount:
    """DELETE the upstream consent and mark the local row revoked.

    ``skip_upstream`` lets a local-only soft-revoke proceed when SISS
    is misconfigured or unreachable; the admin UI exposes it as a
    "remove locally only" fallback so a stuck row can always be cleared.
    """
    row = await session.get(BankFeedAccount, bank_feed_account_id)
    if row is None:
        raise ValueError(f"BankFeedAccount {bank_feed_account_id} not found")
    if not skip_upstream:
        try:
            async with siss_client(settings) as client:
                await endpoints.revoke_account(
                    client, account_id=row.sds_account_id
                )
        except SissNotConfiguredError:
            logger.warning(
                "Local-only revoke for bank_feed_account %s (SISS not configured)",
                row.id,
            )
    row.revoked_at = datetime.now()
    await session.flush()
    return row


# ---------------------------------------------------------------------- #
# Whole-company offboard (Batch K)                                       #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class OffboardResult:
    client_deleted_upstream: bool
    accounts_revoked_locally: int
    export_path: str | None


async def offboard_company(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    export_dir: str | None = None,
    settings: Settings | None = None,
    skip_upstream: bool = False,
) -> OffboardResult:
    """Offboard a whole company's SISS presence.

    1. (optional) Write a CDR-compliance JSON export of every stored
       statement line + account metadata to ``export_dir`` so the user
       has their data before we cut ties upstream.
    2. ``DELETE /sds/clients/{sdsClientId}`` upstream (plan Q5 default
       is soft-revoke-only; pass ``hard_delete`` via the admin UI to
       call this).
    3. Stamp ``revoked_at`` on every local BankFeedAccount for the
       company so future syncs skip them.
    """
    bfc = (
        await session.execute(
            select(BankFeedClient).where(BankFeedClient.company_id == company_id)
        )
    ).scalar_one_or_none()
    if bfc is None:
        return OffboardResult(False, 0, None)

    # Step 1 — CDR export
    export_path: str | None = None
    if export_dir is not None:
        from saebooks.services.bank_feeds import export as export_mod

        export_path = await export_mod.write_cdr_export(
            session, company_id=company_id, export_dir=export_dir
        )

    # Step 2 — upstream delete (if configured + requested)
    deleted_upstream = False
    if not skip_upstream:
        try:
            async with siss_client(settings) as client:
                await endpoints.delete_client(
                    client, sds_client_id=bfc.sds_client_id
                )
            deleted_upstream = True
        except SissNotConfiguredError:
            logger.warning(
                "Local-only offboard for company %s (SISS not configured)",
                company_id,
            )

    # Step 3 — local soft-revoke on every account
    accounts = (
        await session.execute(
            select(BankFeedAccount).where(BankFeedAccount.company_id == company_id)
        )
    ).scalars().all()
    now = datetime.now()
    for acct in accounts:
        if acct.revoked_at is None:
            acct.revoked_at = now
    bfc.active = False
    await session.flush()
    return OffboardResult(
        client_deleted_upstream=deleted_upstream,
        accounts_revoked_locally=len(accounts),
        export_path=export_path,
    )


# ---------------------------------------------------------------------- #
# Sync — pull new transactions and insert locally (Batch I / J)          #
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SyncOutcome:
    bank_feed_account_id: uuid.UUID
    transactions_seen: int
    lines_inserted: int
    cursor_advanced_to: str | None


async def sync_account(
    session: AsyncSession,
    *,
    bank_feed_account_id: uuid.UUID,
    settings: Settings | None = None,
    max_pages: int = 50,
) -> SyncOutcome:
    """Fetch new transactions for one feed account, insert dedup, advance cursor.

    Walks SISS pagination via ``iter_transactions``, batching for
    efficient ``insert_statement_lines`` calls. ``max_pages`` is a
    safety cap; SISS pagination includes ``links.next``, so a
    pathological upstream loop stops after ``max_pages * page_size``
    transactions.
    """
    row = await session.get(BankFeedAccount, bank_feed_account_id)
    if row is None:
        raise ValueError(f"BankFeedAccount {bank_feed_account_id} not found")
    if row.revoked_at is not None:
        return SyncOutcome(bank_feed_account_id, 0, 0, None)

    # Resolve the parent client so we know which sds_client_id to ask
    bfc = await session.get(BankFeedClient, row.bank_feed_client_id)
    if bfc is None or not bfc.active:
        return SyncOutcome(bank_feed_account_id, 0, 0, None)

    buffered: list[dict[str, Any]] = []
    last_txn_id: str | None = row.last_transaction_posted_id
    total_seen = 0

    async with siss_client(settings) as client:
        aiter = endpoints.iter_transactions(
            client,
            sds_client_id=bfc.sds_client_id,
            from_transaction_id=row.last_transaction_posted_id,
            from_transaction_id_is_inclusive=False,
            page_size=100,
        )
        page_count = 0
        async for txn in aiter:
            # The caller's perspective: only transactions for *this*
            # bank_feed_account_id matter. list_transactions is per-
            # client, so we filter on accountId here.
            if txn.get("accountId") and txn.get("accountId") != row.sds_account_id:
                continue
            buffered.append(txn)
            total_seen += 1
            if txn.get("transactionId"):
                last_txn_id = str(txn["transactionId"])
            # Rough batching: flush per ~100 txns to keep insert sizes bounded
            if len(buffered) >= 100:
                page_count += 1
                if page_count > max_pages:
                    break

    inserted = 0
    if buffered:
        inserted = await repo.insert_statement_lines(
            session,
            bank_feed_account_id=bank_feed_account_id,
            transactions=buffered,
        )
    if last_txn_id and last_txn_id != row.last_transaction_posted_id:
        await repo.update_sync_cursor(
            session,
            bank_feed_account_id=bank_feed_account_id,
            last_transaction_posted_id=last_txn_id,
            last_transaction_posted_date=None,
        )
    bfc.last_sync_at = datetime.now()
    await session.flush()
    return SyncOutcome(
        bank_feed_account_id=bank_feed_account_id,
        transactions_seen=total_seen,
        lines_inserted=inserted,
        cursor_advanced_to=last_txn_id,
    )


async def sync_all_active(
    session: AsyncSession,
    *,
    company_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> list[SyncOutcome]:
    """Sync every active BankFeedAccount (optionally scoped to one company).

    Called by the ``sync-feeds`` CLI and by the "Sync now" button on
    the admin UI (scoped to that company). Skips revoked accounts and
    any whose parent BankFeedClient is inactive.
    """
    query = select(BankFeedAccount).where(BankFeedAccount.revoked_at.is_(None))
    if company_id is not None:
        query = query.where(BankFeedAccount.company_id == company_id)
    rows = (await session.execute(query)).scalars().all()
    outcomes: list[SyncOutcome] = []
    for row in rows:
        try:
            outcome = await sync_account(
                session,
                bank_feed_account_id=row.id,
                settings=settings,
            )
            outcomes.append(outcome)
        except (SissError, SissNotConfiguredError) as exc:
            logger.warning(
                "Sync failed for bank_feed_account %s: %s",
                row.id,
                exc,
            )
            outcomes.append(
                SyncOutcome(
                    bank_feed_account_id=row.id,
                    transactions_seen=0,
                    lines_inserted=0,
                    cursor_advanced_to=None,
                )
            )
    return outcomes
