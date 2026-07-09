"""Bank-account service — view over ``accounts`` whose ``account_kind`` is bank-side.

Design (a): bank accounts are not a separate table.  They are ``Account``
rows whose ``account_kind`` is one of BANK_CHECKING / BANK_SAVINGS /
CREDIT_CARD / BANK_LOAN / CASH.  ``account_type`` is derived from the
kind: ASSET for BANK_* and CASH (debit-normal), LIABILITY for
CREDIT_CARD and BANK_LOAN (credit-normal).

Pre-0119 this service filtered on ``bsb IS NOT NULL``, which silently
excluded credit cards (no BSB), loans, and pure-cash accounts.  After
0119, ``account_kind`` is the canonical bank-side signal and BSB is
optional (only banks have one).

Optimistic locking, change_log, and tenant scoping follow the same
conventions as every other API-tier service.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.services import change_log as change_log_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Allowlist of account_kind values that classify an Account row as bank-side.
_BANK_KINDS: tuple[str, ...] = (
    "BANK_CHECKING",
    "BANK_SAVINGS",
    "CREDIT_CARD",
    "BANK_LOAN",
    "CASH",
)

# Kinds whose natural account_type is LIABILITY (credit-normal); all
# other bank kinds default to ASSET (debit-normal).
_LIABILITY_KINDS: frozenset[str] = frozenset({"CREDIT_CARD", "BANK_LOAN"})

# Sentinel for api_update: distinguishes "omit (no change)" from "set NULL".
_UNSET: object = object()


def _kind_to_account_type(kind: str) -> AccountType:
    """Map account_kind → AccountType (debit/credit-normal classification)."""
    return AccountType.LIABILITY if kind in _LIABILITY_KINDS else AccountType.ASSET


# Columns written to change_log.payload for bank-account operations.
_BA_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "tenant_id",
    "code",
    "name",
    "account_type",
    "account_kind",
    "bsb",
    "bank_account_number",
    "bank_account_title",
    "apca_user_id",
    "bank_abbreviation",
    "is_trust_account",
    "show_on_invoice",
    "credit_limit",
    "credit_limit_kind",
    "version",
    "created_at",
    "archived_at",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BankAccountError(ValueError):
    """Raised on bank-account validation or state-transition failure."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the stored value."""

    def __init__(self, current: Account) -> None:
        super().__init__(
            f"BankAccount {current.id} is at version {current.version}, "
            "not the expected version"
        )
        self.current = current


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_bank_account(account: Account) -> bool:
    """True if this account has been classified as a bank-side account.

    Header (parent) accounts are excluded even if they happen to carry
    a bank kind — they're organisational nodes, not real transacting
    accounts. Same defensive check is mirrored in ``_bank_account_filter``.
    """
    return account.account_kind in _BANK_KINDS and not account.is_header


def _serialise(account: Account) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _BA_COLUMNS:
        val = getattr(account, key, None)
        if isinstance(val, (uuid.UUID, Decimal)):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


def _bank_account_filter(company_id: uuid.UUID):
    """WHERE clause that selects bank-account rows.

    Excludes header accounts even if they carry a bank kind — they're
    parents in the CoA tree, not transacting accounts. See _is_bank_account.
    """
    return [
        Account.company_id == company_id,
        Account.archived_at.is_(None),
        Account.account_kind.in_(_BANK_KINDS),
        Account.is_header.is_(False),
    ]


async def _clear_show_on_invoice_siblings(
    session: AsyncSession,
    company_id: uuid.UUID,
    keep_id: uuid.UUID,
) -> None:
    """Enforce the single-flag invariant: at most ONE account per company
    carries ``show_on_invoice``. Clears the flag on every other account."""
    await session.execute(
        sa_update(Account)
        .where(
            Account.company_id == company_id,
            Account.id != keep_id,
            Account.show_on_invoice.is_(True),
        )
        .values(show_on_invoice=False)
    )


async def get_remittance_account(
    session: AsyncSession,
    company_id: uuid.UUID,
) -> Account | None:
    """Return the company's bank account flagged ``show_on_invoice``, or None.

    Deterministic when the invariant is ever violated (e.g. concurrent
    writes): picks the first by account code.
    """
    result = await session.execute(
        select(Account)
        .where(
            Account.company_id == company_id,
            Account.archived_at.is_(None),
            Account.show_on_invoice.is_(True),
        )
        .order_by(Account.code)
        .limit(1)
    )
    return result.scalars().first()


def remit_bank_details(company: Any, bank_account: Any) -> dict[str, str]:
    """Resolve the Remit-to bank details rendered on invoice/credit-note PDFs.

    Precedence: (a) the bank account flagged ``show_on_invoice`` (its ABA
    fields), then (b) the company's static ``bank_*`` columns (0168). Note
    ``bank_abbreviation`` is the 3-letter ABA code (CBA/ANZ/…) — the closest
    thing ``accounts`` has to a bank name.
    """
    if bank_account is not None and bank_account.bank_account_number:
        return {
            "name":           bank_account.bank_abbreviation or "",
            "bsb":            bank_account.bsb or "",
            "account_number": bank_account.bank_account_number or "",
            "account_name":   bank_account.bank_account_title or bank_account.name or "",
        }
    if company is not None:
        return {
            "name":           company.bank_name or "",
            "bsb":            company.bank_bsb or "",
            "account_number": company.bank_account_number or "",
            "account_name":   company.bank_account_name or "",
        }
    return {"name": "", "bsb": "", "account_number": "", "account_name": ""}


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def list_active(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Account], int]:
    """Return (bank_accounts, total_count) — active (non-archived) only."""
    where = _bank_account_filter(company_id)

    count_stmt = select(sa_func.count()).select_from(Account).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Account)
        .where(*where)
        .order_by(Account.code)
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


async def api_get(
    session: AsyncSession,
    bank_account_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
) -> Account | None:
    """Fetch a single bank account. Returns None if not found or not a bank acct.

    When ``tenant_id`` is supplied the lookup is filtered by tenant —
    a foreign-tenant id returns ``None`` even if the row exists.

    When ``company_id`` is supplied the lookup is also filtered by
    company — a sibling-company id within the same tenant returns
    ``None``. This is the cross-company isolation guard (Layer 2
    fix, 2026-05-24): without it, a user "in" company A can read a
    row belonging to company B just because both companies share a
    tenant.
    """
    if tenant_id is not None or company_id is not None:
        clauses = [Account.id == bank_account_id]
        if tenant_id is not None:
            clauses.append(Account.tenant_id == tenant_id)
        if company_id is not None:
            clauses.append(Account.company_id == company_id)
        result = await session.execute(select(Account).where(*clauses))
        account = result.scalars().first()
    else:
        account = await session.get(Account, bank_account_id)
    if account is None or not _is_bank_account(account):
        return None
    return account


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def api_create(
    session: AsyncSession,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor: str,
    *,
    code: str,
    name: str,
    account_kind: str = "BANK_CHECKING",
    bsb: str | None = None,
    bank_account_number: str | None = None,
    bank_account_title: str | None = None,
    apca_user_id: str | None = None,
    bank_abbreviation: str | None = None,
    is_trust_account: bool = False,
    show_on_invoice: bool = False,
    credit_limit: Decimal | None = None,
    credit_limit_kind: str = "soft",
) -> Account:
    """Create a new bank-side account row.

    ``account_kind`` controls both the classification and the derived
    ``account_type`` (LIABILITY for CREDIT_CARD/BANK_LOAN, ASSET for the
    rest).  ``bsb`` is required for BANK_CHECKING / BANK_SAVINGS — every
    other kind treats it as optional.
    """
    if account_kind not in _BANK_KINDS:
        raise BankAccountError(
            f"Invalid account_kind '{account_kind}'. "
            f"Must be one of: {', '.join(_BANK_KINDS)}"
        )
    if account_kind in ("BANK_CHECKING", "BANK_SAVINGS") and not bsb:
        raise BankAccountError(
            f"BSB is required when account_kind is {account_kind}"
        )

    # Import here to avoid circular imports at module level
    from saebooks.services import accounts as accounts_svc

    account = await accounts_svc.create(
        session,
        company_id,
        code=code,
        name=name,
        account_type=_kind_to_account_type(account_kind),
        reconcile=True,
        is_trust_account=is_trust_account,
        tenant_id=tenant_id,
        actor=actor,
        skip_validation=True,
    )
    # Patch the bank-specific fields the accounts.create() doesn't expose
    if credit_limit_kind not in ("soft", "hard"):
        raise BankAccountError(
            f"Invalid credit_limit_kind {credit_limit_kind!r}. Must be soft or hard."
        )
    account.account_kind = account_kind
    account.bsb = bsb
    account.bank_account_number = bank_account_number
    account.bank_account_title = bank_account_title
    account.apca_user_id = apca_user_id
    account.bank_abbreviation = bank_abbreviation
    account.show_on_invoice = show_on_invoice
    if show_on_invoice:
        await _clear_show_on_invoice_siblings(session, company_id, account.id)
    account.credit_limit = credit_limit
    account.credit_limit_kind = credit_limit_kind
    # accounts.create already committed; open a new flush for the extra fields
    await session.flush()
    await session.refresh(account)

    # Overwrite the "create" change_log row written by accounts.create()
    # with one branded as bank_account so queries against entity='bank_account' work.
    await change_log_svc.append(
        session,
        entity="bank_account",
        entity_id=account.id,
        op="created",
        actor=actor,
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
    await session.refresh(account)
    return account


async def api_update(
    session: AsyncSession,
    bank_account_id: uuid.UUID,
    actor: str,
    expected_version: int,
    *,
    code: str | None = None,
    name: str | None = None,
    account_kind: str | None = None,
    bsb: str | None = None,
    bank_account_number: str | None = None,
    bank_account_title: str | None = None,
    apca_user_id: str | None = None,
    bank_abbreviation: str | None = None,
    is_trust_account: bool | None = None,
    show_on_invoice: bool | None = None,
    credit_limit: Decimal | None = _UNSET,
    credit_limit_kind: str | None = None,
) -> Account:
    """Update bank-account fields with optimistic locking + change_log.

    ``credit_limit`` uses a sentinel default (``_UNSET``) so that an
    explicit ``None`` clears the limit while omitting the field leaves it
    unchanged. ``credit_limit_kind`` is only applied when non-None.
    """
    account = await session.get(Account, bank_account_id)
    if account is None or not _is_bank_account(account):
        raise BankAccountError(f"BankAccount {bank_account_id} not found")
    if account.version != expected_version:
        raise VersionConflict(account)

    if code is not None:
        account.code = code.strip()
    if name is not None:
        account.name = name.strip()
    if account_kind is not None:
        if account_kind not in _BANK_KINDS:
            raise BankAccountError(
                f"Invalid account_kind '{account_kind}'. "
                f"Must be one of: {', '.join(_BANK_KINDS)}"
            )
        account.account_kind = account_kind
        # Keep account_type aligned with the kind's debit/credit normality.
        account.account_type = _kind_to_account_type(account_kind)
    if bsb is not None:
        account.bsb = bsb
    if bank_account_number is not None:
        account.bank_account_number = bank_account_number
    if bank_account_title is not None:
        account.bank_account_title = bank_account_title
    if apca_user_id is not None:
        account.apca_user_id = apca_user_id
    if bank_abbreviation is not None:
        account.bank_abbreviation = bank_abbreviation
    if is_trust_account is not None:
        account.is_trust_account = is_trust_account
    if show_on_invoice is not None:
        account.show_on_invoice = show_on_invoice
        if show_on_invoice:
            await _clear_show_on_invoice_siblings(
                session, account.company_id, account.id
            )
    if credit_limit is not _UNSET:
        account.credit_limit = credit_limit
    if credit_limit_kind is not None:
        if credit_limit_kind not in ("soft", "hard"):
            raise BankAccountError(
                f"Invalid credit_limit_kind {credit_limit_kind!r}. Must be soft or hard."
            )
        account.credit_limit_kind = credit_limit_kind

    account.version = account.version + 1
    await session.flush()
    await session.refresh(account)

    await change_log_svc.append(
        session,
        entity="bank_account",
        entity_id=account.id,
        op="updated",
        actor=actor,
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
    await session.refresh(account)
    return account


async def api_delete(
    session: AsyncSession,
    bank_account_id: uuid.UUID,
    actor: str,
    expected_version: int,
) -> Account:
    """Soft-archive a bank account with optimistic locking + change_log."""
    account = await session.get(Account, bank_account_id)
    if account is None or not _is_bank_account(account):
        raise BankAccountError(f"BankAccount {bank_account_id} not found")
    if account.version != expected_version:
        raise VersionConflict(account)

    account.archived_at = datetime.now(UTC)
    account.version = account.version + 1
    await session.flush()
    await session.refresh(account)

    await change_log_svc.append(
        session,
        entity="bank_account",
        entity_id=account.id,
        op="deleted",
        actor=actor,
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
    return account


__all__ = [
    "BankAccountError",
    "VersionConflict",
    "api_create",
    "api_delete",
    "api_get",
    "api_update",
    "get_remittance_account",
    "list_active",
    "remit_bank_details",
]
