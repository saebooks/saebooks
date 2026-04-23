"""Account service — CRUD, dependency check, migrate, delete.

Account code structure (structured numbering mode):
  {prefix}-{children}[-{bustard}]

  - prefix:   registered range code (any width — 1, 10, 200, etc.)
  - children: 1-5 digits defining the hierarchy within the range
  - bustard:  single letter after second hyphen — the "come on you
              bastard, just one more level" overflow when 5 isn't enough

The prefix is explicitly separated by the first hyphen, making range
matching unambiguous. Examples:
  1-0000    = Assets header
  1-1110    = Bank account (prefix 1, children 1110)
  2-1310    = GST Collected (prefix 2, children 1310)
  10-110    = Extended-mode range (prefix 10, children 110)
  1-1234-a  = Bustard overflow

When structured_numbering is OFF (company setting), codes are freeform
text — no validation, no auto-parent from prefix.
"""
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account, AccountType
from saebooks.models.account_range import AccountRange
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.journal_template import JournalTemplate
from saebooks.services import audit as audit_svc
from saebooks.services import change_log as change_log_svc
from saebooks.services import settings as settings_svc

class VersionConflict(Exception):
    """Raised when ``expected_version`` does not match the stored value.

    The API layer catches this and returns 409 with current server state.
    """

    def __init__(self, current: Account) -> None:
        super().__init__(
            f"Account {current.id} is at version {current.version}, not the expected version"
        )
        self.current = current


# Columns serialised into change_log.payload
_ACCOUNT_COLUMNS: tuple[str, ...] = (
    "id",
    "company_id",
    "code",
    "name",
    "account_type",
    "parent_id",
    "tax_code_default",
    "is_header",
    "reconcile",
    "system_managed",
    "bsb",
    "bank_account_number",
    "bank_account_title",
    "apca_user_id",
    "bank_abbreviation",
    "version",
    "created_at",
    "archived_at",
)


def _serialise(account: Account) -> dict[str, Any]:
    """Row → JSON-safe dict for change_log.payload."""
    data: dict[str, Any] = {}
    for key in _ACCOUNT_COLUMNS:
        val = getattr(account, key)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif hasattr(val, "value"):  # StrEnum
            val = val.value
        data[key] = val
    return data


# Max child levels (digits after prefix, before the bustard)
MAX_CHILD_LEVELS = 5

# Structural accounts that get auto-snapshotted before any edit
_PROTECTED_CODES = {"3-8000", "3-9000", "3-9999"}

# Regex: {prefix}-{children}[-{bustard}]
# prefix = one or more digits, children = one or more digits, bustard = optional letter
CODE_PATTERN = re.compile(r"^(\d+)-(\d+)(?:-([a-zA-Z]))?$")

# Default ranges seeded for new companies (Australian standard)
DEFAULT_RANGES: list[dict[str, Any]] = [
    {"prefix": "1", "label": "Assets", "account_types": ["ASSET"], "sort_order": 10},
    {"prefix": "2", "label": "Liabilities", "account_types": ["LIABILITY", "ASSET"], "sort_order": 20},
    {"prefix": "3", "label": "Equity", "account_types": ["EQUITY"], "sort_order": 30},
    {"prefix": "4", "label": "Income", "account_types": ["INCOME", "OTHER_INCOME"], "sort_order": 40},
    {"prefix": "5", "label": "Cost of sales", "account_types": ["COST_OF_SALES"], "sort_order": 50},
    {"prefix": "6", "label": "Expenses", "account_types": ["EXPENSE", "OTHER_INCOME", "INCOME"], "sort_order": 60},
    {"prefix": "8", "label": "Other income", "account_types": ["OTHER_INCOME", "INCOME"], "sort_order": 70},
    {"prefix": "9", "label": "Other expenses", "account_types": ["EXPENSE", "OTHER_EXPENSE"], "sort_order": 80},
]


# ---------------------------------------------------------------------------
# Code parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedCode:
    """Result of parsing an account code against registered ranges."""
    raw: str
    prefix: str          # the matched range prefix
    children: str        # the child digits after the prefix (up to 5)
    bustard: str         # single letter after hyphen, or ""
    depth: int           # 0 = range header, 1-5 = child level, 6 = bustard
    range_label: str     # label from the matched range
    allowed_types: list[str]  # allowed AccountType values from the range


def parse_code(code: str, ranges: list[AccountRange]) -> ParsedCode | None:
    """Parse an account code against registered ranges.

    Returns None if the code doesn't match any range or is malformed.
    The prefix is explicitly the part before the first hyphen.
    """
    match = CODE_PATTERN.match(code.strip())
    if not match:
        return None

    prefix = match.group(1)
    children = match.group(2)
    bustard = match.group(3) or ""

    if len(children) > MAX_CHILD_LEVELS:
        return None  # too many child levels

    # Bustard is only valid at max child depth
    if bustard and len(children) < MAX_CHILD_LEVELS:
        return None  # bustard only allowed at the deepest child level

    # Match prefix against ranges
    for rng in ranges:
        if rng.prefix == prefix:
            # Depth = number of significant child digits (strip trailing zeros)
            # "0000" → 0 (header), "1000" → 1, "1100" → 2, "1110" → 3, "1111" → 4
            stripped = children.rstrip("0")
            depth = len(stripped) + (1 if bustard else 0)

            return ParsedCode(
                raw=code.strip(),
                prefix=rng.prefix,
                children=children,
                bustard=bustard,
                depth=depth,
                range_label=rng.label,
                allowed_types=list(rng.account_types),
            )

    return None


# ---------------------------------------------------------------------------
# Range management
# ---------------------------------------------------------------------------

async def get_ranges(
    session: AsyncSession, company_id: uuid.UUID
) -> list[AccountRange]:
    """Get all account ranges for a company, sorted by sort_order."""
    result = await session.execute(
        select(AccountRange)
        .where(AccountRange.company_id == company_id)
        .order_by(AccountRange.sort_order, AccountRange.prefix)
    )
    return list(result.scalars().all())


async def get_prefix_mode(session: AsyncSession) -> str:
    """Get the current prefix mode: 'classic' (1-9) or 'extended' (any width)."""
    val = await settings_svc.get(session, "prefix_mode")
    if isinstance(val, str) and val in ("classic", "extended"):
        return val
    return "classic"


async def create_range(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    prefix: str,
    label: str,
    account_types: list[str],
    sort_order: int = 0,
) -> AccountRange:
    """Create a new account range."""
    prefix = prefix.strip()
    if not prefix.isdigit():
        raise ValueError("Range prefix must be numeric only.")

    # Enforce prefix_mode setting
    mode = await get_prefix_mode(session)
    if mode == "classic" and len(prefix) != 1:
        raise ValueError(
            "Classic numbering mode is active — prefixes must be a single digit (1-9). "
            "Switch to extended mode in Settings > Chart of accounts to use multi-digit prefixes."
        )
    if mode == "classic" and prefix == "0":
        raise ValueError("Prefix '0' is not valid. Use digits 1-9.")

    # Check for prefix conflicts (one prefix can't be a prefix of another)
    existing = await get_ranges(session, company_id)
    for rng in existing:
        if (rng.prefix.startswith(prefix) or prefix.startswith(rng.prefix)) and rng.prefix != prefix:
            raise ValueError(
                f"Prefix '{prefix}' conflicts with existing range "
                f"'{rng.prefix}' ({rng.label}). One cannot be a prefix of the other."
            )

    # Validate account types
    valid_types = {t.value for t in AccountType}
    for at in account_types:
        if at not in valid_types:
            raise ValueError(f"Invalid account type: {at}")

    rng = AccountRange(
        company_id=company_id,
        prefix=prefix,
        label=label.strip(),
        account_types=account_types,
        sort_order=sort_order,
    )
    session.add(rng)
    await session.commit()
    await session.refresh(rng)
    return rng


async def update_range(
    session: AsyncSession,
    range_id: uuid.UUID,
    *,
    label: str | None = None,
    account_types: list[str] | None = None,
    sort_order: int | None = None,
) -> AccountRange:
    """Update an existing account range. Prefix cannot be changed."""
    rng = await session.get(AccountRange, range_id)
    if rng is None:
        raise ValueError(f"Range {range_id} not found")
    if label is not None:
        rng.label = label.strip()
    if account_types is not None:
        valid_types = {t.value for t in AccountType}
        for at in account_types:
            if at not in valid_types:
                raise ValueError(f"Invalid account type: {at}")
        rng.account_types = account_types
    if sort_order is not None:
        rng.sort_order = sort_order
    await session.commit()
    await session.refresh(rng)
    return rng


async def delete_range(session: AsyncSession, range_id: uuid.UUID) -> None:
    """Delete an account range. Doesn't delete the accounts — just the range definition."""
    rng = await session.get(AccountRange, range_id)
    if rng is None:
        raise ValueError(f"Range {range_id} not found")
    await session.delete(rng)
    await session.commit()


async def seed_default_ranges(
    session: AsyncSession, company_id: uuid.UUID
) -> list[AccountRange]:
    """Seed the default Australian account ranges for a company.

    Idempotent — skips ranges that already exist.
    """
    existing = await get_ranges(session, company_id)
    existing_prefixes = {r.prefix for r in existing}
    created = []

    for dflt in DEFAULT_RANGES:
        if dflt["prefix"] not in existing_prefixes:
            rng = AccountRange(
                company_id=company_id,
                prefix=dflt["prefix"],
                label=dflt["label"],
                account_types=dflt["account_types"],
                sort_order=dflt["sort_order"],
            )
            session.add(rng)
            created.append(rng)

    if created:
        await session.commit()
        for rng in created:
            await session.refresh(rng)

    return created


# ---------------------------------------------------------------------------
# Code validation (structured numbering mode)
# ---------------------------------------------------------------------------

async def validate_code(
    session: AsyncSession,
    company_id: uuid.UUID,
    code: str,
    account_type: AccountType,
) -> list[str]:
    """Validate an account code against registered ranges.

    Returns list of error messages (empty = OK).
    """
    errors: list[str] = []
    code = code.strip()

    if not code:
        errors.append("Account code is required.")
        return errors

    match = CODE_PATTERN.match(code)
    if not match:
        errors.append(
            "Account code must be {prefix}-{digits} format, "
            "optionally with a letter suffix e.g. 1-1234-a."
        )
        return errors

    ranges = await get_ranges(session, company_id)
    if not ranges:
        # No ranges defined — skip range validation
        return errors

    parsed = parse_code(code, ranges)
    if parsed is None:
        prefix = match.group(1)
        children = match.group(2)
        bustard = match.group(3) or ""

        if len(children) > MAX_CHILD_LEVELS:
            errors.append(
                f"Too many child levels. Maximum is {MAX_CHILD_LEVELS} "
                f"child digits, got {len(children)}."
            )
            return errors

        if bustard and len(children) < MAX_CHILD_LEVELS:
            errors.append(
                f"The letter suffix is only allowed at child level "
                f"{MAX_CHILD_LEVELS}. Current depth: {len(children)}."
            )
            return errors

        # No matching range
        prefixes = ", ".join(r.prefix for r in sorted(ranges, key=lambda r: r.sort_order))
        errors.append(
            f"Prefix '{prefix}' doesn't match any account range. "
            f"Defined ranges: {prefixes}."
        )
        return errors

    # Check account type against range
    if account_type.value not in parsed.allowed_types:
        type_names = ", ".join(
            t.replace("_", " ").title() for t in sorted(parsed.allowed_types)
        )
        errors.append(
            f"Range '{parsed.prefix}' ({parsed.range_label}) requires type: "
            f"{type_names}. Got: {account_type.value.replace('_', ' ').title()}."
        )

    return errors


def check_code_anomaly(
    code: str, account_type: AccountType, ranges: list[AccountRange]
) -> str | None:
    """Check if an existing account's code doesn't match the expected type range.

    Returns a warning string if there's a mismatch, None otherwise.
    """
    if not ranges:
        return None

    parsed = parse_code(code, ranges)
    if parsed is None:
        return f"Code '{code}' doesn't match any defined range"

    if account_type.value not in parsed.allowed_types:
        return (
            f"Range '{parsed.prefix}' ({parsed.range_label}) expects "
            f"{', '.join(parsed.allowed_types)} but account is {account_type.value}"
        )
    return None


async def find_parent(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> Account | None:
    """Find the parent account by progressively shortening children digits.

    For code "1-1234", tries "1-1230", "1-1200", "1-1000", "1-0000" in order.
    For bustard codes like "1-1234-a", tries "1-1234" first (without bustard).
    Returns the first existing account that matches.
    """
    match = CODE_PATTERN.match(code)
    if not match:
        return None

    prefix = match.group(1)
    children = match.group(2)
    bustard = match.group(3) or ""

    # If bustard code, parent is the same code without bustard
    if bustard:
        candidate = f"{prefix}-{children}"
        result = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == candidate,
                Account.archived_at.is_(None),
            )
        )
        parent = result.scalars().first()
        if parent is not None:
            return parent

    # Walk children digits from right to left, zeroing each position
    for i in range(len(children) - 1, -1, -1):
        candidate_children = children[:i] + "0" * (len(children) - i)
        candidate = f"{prefix}-{candidate_children}"
        if candidate == code:
            continue  # skip self
        result = await session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == candidate,
                Account.archived_at.is_(None),
            )
        )
        parent = result.scalars().first()
        if parent is not None:
            return parent

    return None


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


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
    skip_validation: bool = False,
    actor: str = "web",
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> Account:
    code = code.strip()

    if not skip_validation:
        errors = await validate_code(session, company_id, code, account_type)
        if errors:
            raise ValueError("; ".join(errors))

    # Auto-derive parent from code prefix
    parent = await find_parent(session, company_id, code)

    account = Account(
        company_id=company_id,
        tenant_id=tenant_id,
        code=code,
        name=name.strip(),
        account_type=account_type,
        parent_id=parent.id if parent else None,
        reconcile=reconcile,
        is_header=is_header,
        tax_code_default=tax_code_default,
        version=1,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    await change_log_svc.append(
        session,
        entity="account",
        entity_id=account.id,
        op="create",
        actor=actor,
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
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
    skip_validation: bool = False,
    performed_by: str | None = None,
    actor: str | None = None,
    expected_version: int | None = None,
) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")

    if expected_version is not None and account.version != expected_version:
        raise VersionConflict(account)

    new_code = code.strip() if code is not None else account.code
    new_type = account_type if account_type is not None else account.account_type

    if not skip_validation:
        errors = await validate_code(session, account.company_id, new_code, new_type)
        if errors:
            raise ValueError("; ".join(errors))

    # Capture pre-mutation state for protected/system accounts so we can
    # record a proper before→after audit snapshot.
    should_snapshot = account.system_managed or account.code in _PROTECTED_CODES
    before_data = audit_svc.capture(account) if should_snapshot else None

    if code is not None:
        account.code = new_code
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

    # Re-derive parent if code changed
    if code is not None:
        parent = await find_parent(session, account.company_id, new_code)
        account.parent_id = parent.id if parent else None

    account.version = account.version + 1

    # Snapshot AFTER mutation, pairing with the captured before-state, so the
    # audit viewer can diff the fields that actually changed.
    if should_snapshot:
        await audit_svc.snapshot_row(
            session, account,
            action="update",
            before_data=before_data,
            performed_by=performed_by,
        )

    await session.flush()
    await session.refresh(account)
    await change_log_svc.append(
        session,
        entity="account",
        entity_id=account.id,
        op="update",
        actor=actor or performed_by or "web",
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
    return account


async def archive(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    actor: str | None = None,
    expected_version: int | None = None,
) -> Account | None:
    account = await session.get(Account, account_id)
    if account is None:
        return None
    if expected_version is not None and account.version != expected_version:
        raise VersionConflict(account)
    account.archived_at = datetime.now(UTC)
    account.version = account.version + 1
    await session.flush()
    await session.refresh(account)
    await change_log_svc.append(
        session,
        entity="account",
        entity_id=account.id,
        op="archive",
        actor=actor or performed_by or "web",
        payload=_serialise(account),
        version=account.version,
    )
    await session.commit()
    return account


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
    *,
    performed_by: str | None = None,
) -> dict[str, int]:
    """Move all references from source account to target account."""
    source = await session.get(Account, source_id)
    target = await session.get(Account, target_id)
    if source is None:
        raise ValueError(f"Source account {source_id} not found")
    if target is None:
        raise ValueError(f"Target account {target_id} not found")

    # Snapshot source account before migration
    await audit_svc.snapshot_row(
        session, source,
        action="migrate",
        reason=f"Migrating all references to {target.code} {target.name}",
        performed_by=performed_by,
    )

    counts: dict[str, int] = {}

    result = await session.execute(
        sa_update(JournalLine)
        .where(JournalLine.account_id == source_id)
        .values(account_id=target_id)
    )
    counts["journal_lines"] = result.rowcount  # type: ignore[attr-defined]

    result = await session.execute(
        sa_update(BankStatementLine)
        .where(BankStatementLine.account_id == source_id)
        .values(account_id=target_id)
    )
    counts["bank_statement_lines"] = result.rowcount  # type: ignore[attr-defined]

    result = await session.execute(
        sa_update(Account)
        .where(Account.parent_id == source_id)
        .values(parent_id=target_id)
    )
    counts["child_accounts"] = result.rowcount  # type: ignore[attr-defined]

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


async def delete_account(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    """Hard-delete an account. Will fail if FK RESTRICT references still exist."""
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")

    # Always snapshot before deletion — this is the only way to recover
    await audit_svc.snapshot_row(
        session, account, action="delete", performed_by=performed_by,
    )

    await session.delete(account)
    await session.commit()
