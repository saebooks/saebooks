"""Bank rule service — CRUD, matching, and applying rules to bank lines.

A rule matches when a bank statement line's description satisfies the
rule's match_pattern under the rule's match_type. The first matching rule
(by priority desc, name asc) wins.

When a rule matches:
- If `auto_create=True`, a posted journal entry is created from the line.
- Otherwise, the rule is just suggested for the user to apply manually.

Generated journal:
- Bank line amount > 0 (deposit): DR bank, CR rule.account_id
- Bank line amount < 0 (withdrawal): DR rule.account_id, CR bank
"""
import re
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bank_rule import BankRule, MatchType
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.journal import JournalEntry, JournalOrigin
from saebooks.models.tax_code import TaxCode
from saebooks.money import money_quantum
from saebooks.services import audit as audit_svc
from saebooks.services import journal as journal_svc

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def list_rules(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    active_only: bool = False,
) -> list[BankRule]:
    """List all rules, ordered by priority desc, name asc."""
    stmt = select(BankRule).where(BankRule.company_id == company_id)
    if active_only:
        stmt = stmt.where(BankRule.is_active.is_(True))
    stmt = stmt.order_by(BankRule.priority.desc(), BankRule.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, rule_id: uuid.UUID) -> BankRule | None:
    return await session.get(BankRule, rule_id)


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str,
    match_pattern: str,
    account_id: uuid.UUID,
    match_type: MatchType = MatchType.CONTAINS,
    tax_code: str | None = None,
    contact_id: uuid.UUID | None = None,
    description_template: str | None = None,
    auto_create: bool = False,
    priority: int = 0,
    is_active: bool = True,
) -> BankRule:
    """Create a new bank rule. Validates regex pattern if match_type=REGEX."""
    name = name.strip()
    pattern = match_pattern.strip()
    if not name:
        raise ValueError("Rule name is required.")
    if not pattern:
        raise ValueError("Match pattern is required.")
    if match_type == MatchType.REGEX:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

    rule = BankRule(
        company_id=company_id,
        name=name,
        match_pattern=pattern,
        match_type=match_type,
        account_id=account_id,
        tax_code=tax_code or None,
        contact_id=contact_id,
        description_template=description_template or None,
        auto_create=auto_create,
        priority=priority,
        is_active=is_active,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


async def update(
    session: AsyncSession,
    rule_id: uuid.UUID,
    *,
    performed_by: str | None = None,
    **kwargs,
) -> BankRule:
    rule = await session.get(BankRule, rule_id)
    if rule is None:
        raise ValueError(f"Rule {rule_id} not found")

    if "match_pattern" in kwargs:
        kwargs["match_pattern"] = kwargs["match_pattern"].strip()
    if "name" in kwargs:
        kwargs["name"] = kwargs["name"].strip()
    if kwargs.get("match_type") == MatchType.REGEX and "match_pattern" in kwargs:
        try:
            re.compile(kwargs["match_pattern"])
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

    allowed = {
        "name", "match_pattern", "match_type", "account_id", "tax_code",
        "contact_id", "description_template", "auto_create", "priority",
        "is_active",
    }
    before = audit_svc.capture(rule)
    for key, value in kwargs.items():
        if key not in allowed:
            raise ValueError(f"Unknown field: {key}")
        setattr(rule, key, value)

    await audit_svc.snapshot_row(
        session, rule,
        action="update",
        before_data=before,
        performed_by=performed_by,
    )
    await session.commit()
    await session.refresh(rule)
    return rule


async def delete(
    session: AsyncSession,
    rule_id: uuid.UUID,
    *,
    performed_by: str | None = None,
) -> None:
    rule = await session.get(BankRule, rule_id)
    if rule is None:
        return
    await audit_svc.snapshot_row(
        session, rule,
        action="delete",
        performed_by=performed_by,
    )
    await session.delete(rule)
    await session.commit()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _matches(rule: BankRule, description: str) -> bool:
    """Test if a rule matches the given description (case-insensitive)."""
    if not description:
        return False
    desc = description.lower()
    pat = rule.match_pattern.lower()

    if rule.match_type == MatchType.CONTAINS:
        return pat in desc
    if rule.match_type == MatchType.STARTS_WITH:
        return desc.startswith(pat)
    if rule.match_type == MatchType.EXACT:
        return desc == pat
    if rule.match_type == MatchType.REGEX:
        try:
            return bool(re.search(rule.match_pattern, description, re.IGNORECASE))
        except re.error:
            return False
    return False


async def find_matching_rule(
    session: AsyncSession,
    company_id: uuid.UUID,
    description: str,
) -> BankRule | None:
    """Return the highest-priority active rule that matches the description."""
    rules = await list_rules(session, company_id, active_only=True)
    for rule in rules:
        if _matches(rule, description):
            return rule
    return None


async def preview_matches(
    session: AsyncSession,
    company_id: uuid.UUID,
    rule: BankRule,
    *,
    limit: int = 20,
) -> list[BankStatementLine]:
    """Find all unmatched bank lines that would be matched by this rule.
    Useful for showing 'this rule would match N existing lines'.
    """
    stmt = (
        select(BankStatementLine)
        .where(
            BankStatementLine.company_id == company_id,
            BankStatementLine.status == StatementLineStatus.UNMATCHED,
        )
        .order_by(BankStatementLine.txn_date.desc())
        .limit(500)  # Cap candidates we scan
    )
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())
    matched = [
        line for line in candidates
        if _matches(rule, line.description or "")
    ]
    return matched[:limit]


# ---------------------------------------------------------------------------
# Apply rule → create journal entry
# ---------------------------------------------------------------------------

async def _resolve_tax_code(
    session: AsyncSession,
    company_id: uuid.UUID,
    code: str | None,
) -> TaxCode | None:
    if not code:
        return None
    # Home jurisdiction (AU) only — the international reference codes
    # reuse code strings, so an unqualified code match would be ambiguous.
    result = await session.execute(
        select(TaxCode).where(
            TaxCode.company_id == company_id,
            TaxCode.code == code,
            TaxCode.jurisdiction == "AU",
            TaxCode.archived_at.is_(None),
        )
    )
    return result.scalars().first()


def _split_gst(gross: Decimal, rate_pct: Decimal) -> tuple[Decimal, Decimal]:
    """Split a gross GST-inclusive amount into (net, gst).
    rate_pct is the percent (e.g. 10 for 10% GST). Returns rounded to 2dp.
    """
    if rate_pct == Decimal("0"):
        return gross, Decimal("0")
    rate = rate_pct / Decimal("100")
    # gross = net * (1 + rate); gst = gross - net
    net = (gross / (Decimal("1") + rate)).quantize(money_quantum(2))
    gst = (gross - net).quantize(money_quantum(2))
    return net, gst


async def apply_rule_to_line(
    session: AsyncSession,
    line_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    posted_by: str | None = "bank-rule",
) -> JournalEntry:
    """Create + post a journal entry for a bank line using a rule.

    DR bank, CR target account (deposit)  -or-
    DR target account, CR bank (withdrawal)

    Marks the bank line MATCHED with the new entry.
    """
    line = await session.get(BankStatementLine, line_id)
    if line is None:
        raise ValueError(f"Bank statement line {line_id} not found")
    if line.status == StatementLineStatus.MATCHED:
        raise ValueError("Line is already matched")

    rule = await session.get(BankRule, rule_id)
    if rule is None:
        raise ValueError(f"Rule {rule_id} not found")

    # Resolve tax code (need rate for GST split)
    tax_code = await _resolve_tax_code(session, line.company_id, rule.tax_code)
    tax_code_id = tax_code.id if tax_code else None

    gross = abs(line.amount)
    desc = (rule.description_template or line.description or rule.name).strip()

    # Split into net + GST so the GST auto-poster can add the GST account line
    if tax_code is not None and tax_code.rate and tax_code.rate > 0:
        net, gst = _split_gst(gross, tax_code.rate)
    else:
        net, gst = gross, Decimal("0")

    if line.amount >= 0:
        # Deposit: DR bank (gross), CR target (net + gst_amount info)
        lines = [
            {
                "account_id": line.account_id,
                "description": desc,
                "debit": gross,
                "credit": Decimal("0"),
            },
            {
                "account_id": rule.account_id,
                "description": desc,
                "debit": Decimal("0"),
                "credit": net,
                "tax_code_id": tax_code_id,
                "gst_amount": gst if gst > 0 else None,
            },
        ]
    else:
        # Withdrawal: DR target (net + gst_amount info), CR bank (gross)
        lines = [
            {
                "account_id": rule.account_id,
                "description": desc,
                "debit": net,
                "credit": Decimal("0"),
                "tax_code_id": tax_code_id,
                "gst_amount": gst if gst > 0 else None,
            },
            {
                "account_id": line.account_id,
                "description": desc,
                "debit": Decimal("0"),
                "credit": gross,
            },
        ]

    entry = await journal_svc.create_draft(
        session,
        company_id=line.company_id,
        tenant_id=line.tenant_id,
        entry_date=line.txn_date,
        description=f"[Rule: {rule.name}] {desc}",
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        origin=JournalOrigin.BANK_REC,
        source_type="bank_statement_line",
        source_id=line.id,
    )

    # Re-fetch the line in this session and mark matched
    line = await session.get(BankStatementLine, line_id)
    if line is not None:
        line.matched_entry_id = posted.id
        line.status = StatementLineStatus.MATCHED
        line.bank_rule_id = rule.id
        from datetime import datetime
        line.matched_at = datetime.now()
        line.matched_by = posted_by
        await session.commit()

    return posted


async def auto_apply_rules(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    only_account_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Scan all unmatched bank lines for the company and apply any rule with
    auto_create=True. Returns counts: {'matched': N, 'created': M, 'skipped': S}.
    """
    stmt = select(BankStatementLine).where(
        BankStatementLine.company_id == company_id,
        BankStatementLine.status == StatementLineStatus.UNMATCHED,
    )
    if only_account_id is not None:
        stmt = stmt.where(BankStatementLine.account_id == only_account_id)
    result = await session.execute(stmt)
    lines = list(result.scalars().all())

    rules = await list_rules(session, company_id, active_only=True)
    auto_rules = [r for r in rules if r.auto_create]

    counts = {"matched": 0, "created": 0, "skipped": 0}
    for line in lines:
        for rule in auto_rules:
            if _matches(rule, line.description or ""):
                try:
                    await apply_rule_to_line(session, line.id, rule.id)
                    counts["matched"] += 1
                    counts["created"] += 1
                except Exception:
                    counts["skipped"] += 1
                break

    return counts


async def find_suggestions_for_lines(
    session: AsyncSession,
    company_id: uuid.UUID,
    lines: list[BankStatementLine],
) -> dict[uuid.UUID, BankRule]:
    """Build a {line_id: rule} dict suggesting rules for a list of lines.
    Skips lines already matched.
    """
    rules = await list_rules(session, company_id, active_only=True)
    suggestions: dict[uuid.UUID, BankRule] = {}
    for line in lines:
        if line.status == StatementLineStatus.MATCHED:
            continue
        for rule in rules:
            if _matches(rule, line.description or ""):
                suggestions[line.id] = rule
                break
    return suggestions
