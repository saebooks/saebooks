"""Bank feeds — SISS Data Services integration (v1.1).

Public surface for the `bank_feeds` service module. Callers outside this
package should import from here; the submodules are implementation detail
and may be reorganised.

Phase 1 (this module) provides the HTTP client foundation only:

    from saebooks.services.bank_feeds import SissClient, SissError

    client = SissClient.from_settings(settings)
    async with client:
        accounts = await client.get("/sds/clients/abc/accounts", scopes=["sds_clients"])

Business-logic wrappers (onboarding, sync, health) land in later phases.
"""
from saebooks.services.bank_feeds.client import SissClient
from saebooks.services.bank_feeds.endpoints import (
    get_account_detail,
    get_client,
    iter_transactions,
    list_accounts,
    list_clients,
    list_feed_issues,
    list_transactions,
)
from saebooks.services.bank_feeds.errors import (
    SissAuthError,
    SissError,
    SissRateLimitError,
    SissScopeError,
    SissValidationError,
)
from saebooks.services.bank_feeds.repo import (
    get_or_create_client,
    insert_statement_lines,
    update_sync_cursor,
    upsert_bank_feed_account,
    upsert_feed_issue,
)
from saebooks.services.bank_feeds.token import TokenCache

__all__ = [
    "SissAuthError",
    "SissClient",
    "SissError",
    "SissRateLimitError",
    "SissScopeError",
    "SissValidationError",
    "TokenCache",
    "get_account_detail",
    "get_client",
    "get_or_create_client",
    "insert_statement_lines",
    "iter_transactions",
    "list_accounts",
    "list_clients",
    "list_feed_issues",
    "list_transactions",
    "update_sync_cursor",
    "upsert_bank_feed_account",
    "upsert_feed_issue",
]
