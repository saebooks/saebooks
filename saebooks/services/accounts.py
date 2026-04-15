"""Account service — CRUD, dependency check, migrate, delete."""
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.journal_template import JournalTemplate


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


# ---------------------------------------------------------------------------
# Dependency check — what's blocking deletion of this account?
# ---------------------------------------------------------------------------

@dataclass
class JournalLineDep:
    """A journal entry that has lines on this account."""
    entry_id: uuid.UUID
    ref: str
    entry_date: Any  # date
    status: str
    line_count: int


@dataclass
class AccountDependencies:
    """Everything referencing an account."""
    account: Account
    journal_entries: list[JournalLineDep] = field(default_factory=list)
    bank_statement_count: int = 0
    child_accounts: list[Account] = field(default_factory=list)
    templates: list[JournalTemplate] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        """True if hard FK references exist that prevent deletion."""
        return bool(self.journal_entries) or self.bank_statement_count > 0

    @property
    def has_any(self) -> bool:
        return (
            self.has_blockers
            or bool(self.child_accounts)
            or bool(self.templates)
        )

    @property
    def total_journal_lines(self) -> int:
        return sum(d.line_count for d in self.journal_entries)


async def check_dependencies(
    session: AsyncSession, account_id: uuid.UUID
) -> AccountDependencies:
    """Find everything that references this account."""
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")

    deps = AccountDependencies(account=account)

    # 1. Journal entries with lines on this account (grouped by entry)
    stmt = (
        select(
            JournalEntry.id,
            JournalEntry.ref,
            JournalEntry.entry_date,
            JournalEntry.status,
            func.count(JournalLine.id).label("line_count"),
        )
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.account_id == account_id)
        .group_by(JournalEntry.id)
        .order_by(JournalEntry.entry_date.desc())
    )
    for row in (await session.execute(stmt)).all():
        deps.journal_entries.append(JournalLineDep(
            entry_id=row[0],
            ref=row[1],
            entry_date=row[2],
            status=row[3],
            line_count=row[4],
        ))

    # 2. Bank statement lines
    bsl_count = await session.execute(
        select(func.count(BankStatementLine.id)).where(
            BankStatementLine.account_id == account_id
        )
    )
    deps.bank_statement_count = bsl_count.scalar_one()

    # 3. Child accounts (parent_id = this account)
    children = await session.execute(
        select(Account).where(Account.parent_id == account_id)
    )
    deps.child_accounts = list(children.scalars().all())

    # 4. Journal templates referencing this account in JSONB lines
    # Scan all non-archived templates for account_id in their lines array
    acct_str = str(account_id)
    all_templates = await session.execute(
        select(JournalTemplate).where(JournalTemplate.archived_at.is_(None))
    )
    for tmpl in all_templates.scalars().all():
        if tmpl.lines:
            for line in tmpl.lines:
                if line.get("account_id") == acct_str:
                    deps.templates.append(tmpl)
                    break

    return deps


async def migrate_account(
    session: AsyncSession,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> dict[str, int]:
    """Move all references from source account to target account.

    Returns counts of what was migrated.
    """
    source = await session.get(Account, source_id)
    target = await session.get(Account, target_id)
    if source is None:
        raise ValueError(f"Source account {source_id} not found")
    if target is None:
        raise ValueError(f"Target account {target_id} not found")

    counts: dict[str, int] = {}

    # 1. Journal lines
    result = await session.execute(
        sa_update(JournalLine)
        .where(JournalLine.account_id == source_id)
        .values(account_id=target_id)
    )
    counts["journal_lines"] = result.rowcount  # type: ignore[attr-defined]

    # 2. Bank statement lines
    result = await session.execute(
        sa_update(BankStatementLine)
        .where(BankStatementLine.account_id == source_id)
        .values(account_id=target_id)
    )
    counts["bank_statement_lines"] = result.rowcount  # type: ignore[attr-defined]

    # 3. Child accounts
    result = await session.execute(
        sa_update(Account)
        .where(Account.parent_id == source_id)
        .values(parent_id=target_id)
    )
    counts["child_accounts"] = result.rowcount  # type: ignore[attr-defined]

    # 4. Journal templates (JSONB — need to update in Python)
    source_str = str(source_id)
    target_str = str(target_id)
    all_templates = await session.execute(
        select(JournalTemplate).where(JournalTemplate.archived_at.is_(None))
    )
    tmpl_count = 0
    for tmpl in all_templates.scalars().all():
        if tmpl.lines:
            changed = False
            new_lines = []
            for line in tmpl.lines:
                if line.get("account_id") == source_str:
                    line = {**line, "account_id": target_str}
                    changed = True
                new_lines.append(line)
            if changed:
                tmpl.lines = new_lines
                tmpl_count += 1
    counts["templates"] = tmpl_count

    await session.commit()
    return counts


async def delete_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Hard-delete an account. Will fail if FK RESTRICT references still exist."""
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    await session.delete(account)
    await session.commit()
