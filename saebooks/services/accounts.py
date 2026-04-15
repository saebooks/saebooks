import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType


async def list_active(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Account]:
    result = await session.execute(
        select(Account)
        .where(Account.company_id == company_id, Account.archived_at.is_(None))
        .order_by(Account.code)
    )
    return list(result.scalars().all())


async def get(session: AsyncSession, account_id: uuid.UUID) -> Account | None:
    return await session.get(Account, account_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    code: str,
    name: str,
    account_type: AccountType,
    reconcile: bool = False,
    is_header: bool = False,
    tax_code_default: str | None = None,
) -> Account:
    account = Account(
        company_id=company_id,
        code=code.strip(),
        name=name.strip(),
        account_type=account_type,
        reconcile=reconcile,
        is_header=is_header,
        tax_code_default=tax_code_default,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def update(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    code: str | None = None,
    name: str | None = None,
    account_type: AccountType | None = None,
    reconcile: bool | None = None,
    is_header: bool | None = None,
    tax_code_default: str | None = None,
) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    if code is not None:
        account.code = code.strip()
    if name is not None:
        account.name = name.strip()
    if account_type is not None:
        account.account_type = account_type
    if reconcile is not None:
        account.reconcile = reconcile
    if is_header is not None:
        account.is_header = is_header
    if tax_code_default is not None:
        account.tax_code_default = tax_code_default or None
    await session.commit()
    await session.refresh(account)
    return account


async def archive(session: AsyncSession, account_id: uuid.UUID) -> None:
    account = await session.get(Account, account_id)
    if account is None:
        return
    account.archived_at = datetime.now(UTC)
    await session.commit()
