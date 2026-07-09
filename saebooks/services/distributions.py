"""Trust distribution service.

Lifecycle: DRAFT -> MINUTED -> POSTED.

``create``            — save distribution + entitlements (validates % sum).
``minute``            — record resolution_minuted_date, advance to MINUTED.
``post_journal_entry`` — create + post the GL entry, link journal_entry_id,
                         advance to POSTED.
``list_active``       — list non-archived distributions for a company.
``get``               — fetch a single distribution with entitlements.
``delete``            — soft-delete (archived_at).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.models.distribution import (
    BeneficiaryEntitlement,
    DistributionStatus,
    TrustDistribution,
)
from saebooks.models.journal import JournalOrigin
from saebooks.services import journal as journal_svc

_PERCENTAGE_TOLERANCE = Decimal("0.01")


class DistributionError(ValueError):
    """Raised on validation or state-transition failure."""


def _validate_entitlements(entitlements: list[dict]) -> None:
    """Raise DistributionError if percentages do not sum to ~100."""
    if not entitlements:
        raise DistributionError("A distribution must have at least one beneficiary.")
    total = sum(Decimal(str(e["percentage"])) for e in entitlements)
    if abs(total - Decimal("100")) > _PERCENTAGE_TOLERANCE:
        raise DistributionError(
            f"Beneficiary percentages must sum to 100 (got {total:.4f})."
        )


async def list_active(
    session: AsyncSession, company_id: uuid.UUID
) -> list[TrustDistribution]:
    result = await session.execute(
        select(TrustDistribution)
        .where(
            TrustDistribution.company_id == company_id,
            TrustDistribution.archived_at.is_(None),
        )
        .options(selectinload(TrustDistribution.entitlements))
        .order_by(TrustDistribution.financial_year.desc(), TrustDistribution.distribution_date.desc())
    )
    return list(result.scalars().all())


async def get(
    session: AsyncSession, distribution_id: uuid.UUID
) -> TrustDistribution | None:
    result = await session.execute(
        select(TrustDistribution)
        .where(TrustDistribution.id == distribution_id)
        .options(selectinload(TrustDistribution.entitlements))
    )
    return result.scalars().first()


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    financial_year: int,
    distribution_date: date,
    total_amount: Decimal,
    total_franking_credits: Decimal = Decimal("0"),
    notes: str | None,
    entitlements: list[dict],
) -> TrustDistribution:
    """Create a DRAFT distribution with its beneficiary entitlements.

    Each entitlement dict: {beneficiary_name, percentage, amount,
                            account_id?, notes?, franking_credit_amount?}

    If ``franking_credit_amount`` is absent from an entitlement dict,
    it is computed as ``percentage / 100 * total_franking_credits``
    so callers can omit it and let the service gross up automatically.
    """
    _validate_entitlements(entitlements)

    dist = TrustDistribution(
        company_id=company_id,
        financial_year=financial_year,
        distribution_date=distribution_date,
        total_amount=total_amount,
        total_franking_credits=total_franking_credits,
        notes=notes or None,
        status=DistributionStatus.DRAFT,
    )
    session.add(dist)
    await session.flush()

    for i, e in enumerate(entitlements):
        acct_id = e.get("account_id")
        pct = Decimal(str(e["percentage"]))
        # Accept an explicit franking_credit_amount or compute from total.
        if "franking_credit_amount" in e and e["franking_credit_amount"] not in (None, ""):
            fc_amount = Decimal(str(e["franking_credit_amount"]))
        else:
            fc_amount = (pct / Decimal("100") * total_franking_credits).quantize(
                Decimal("0.01")
            )
        session.add(
            BeneficiaryEntitlement(
                distribution_id=dist.id,
                sort_order=i,
                beneficiary_name=str(e["beneficiary_name"]).strip(),
                percentage=pct,
                amount=Decimal(str(e["amount"])),
                franking_credit_amount=fc_amount,
                account_id=uuid.UUID(str(acct_id)) if acct_id else None,
                notes=str(e["notes"]).strip() if e.get("notes") else None,
            )
        )

    await session.commit()
    await session.refresh(dist)
    return dist


async def minute(
    session: AsyncSession,
    distribution_id: uuid.UUID,
    *,
    minuted_date: date,
) -> TrustDistribution:
    dist = await get(session, distribution_id)
    if dist is None:
        raise DistributionError(f"Distribution {distribution_id} not found.")
    if dist.status == DistributionStatus.POSTED:
        raise DistributionError("Cannot update a posted distribution.")
    dist.resolution_minuted_date = minuted_date
    dist.status = DistributionStatus.MINUTED
    await session.commit()
    await session.refresh(dist)
    return dist


async def post_journal_entry(
    session: AsyncSession,
    distribution_id: uuid.UUID,
    *,
    income_account_id: uuid.UUID,
    posted_by: str | None = None,
) -> TrustDistribution:
    """Create and post the distribution JE, then advance status to POSTED.

    The JE structure:
      DR  Trust Income / Retained Earnings  (income_account_id)   total_amount
      CR  Beneficiary Payable — <name>      (entitlement.account_id) amount

    Entitlements without an account_id are posted to the income_account_id
    as a fallback (operator must fix accounts later).
    """
    dist = await get(session, distribution_id)
    if dist is None:
        raise DistributionError(f"Distribution {distribution_id} not found.")
    if dist.status == DistributionStatus.POSTED:
        raise DistributionError("Distribution is already posted.")
    if not dist.entitlements:
        raise DistributionError("No beneficiary entitlements — cannot post.")

    total = dist.total_amount
    lines: list[dict] = [
        {
            "account_id": income_account_id,
            "description": f"Trust distribution {dist.financial_year} — debit",
            "debit": total,
            "credit": Decimal("0"),
        }
    ]
    for ent in dist.entitlements:
        cr_acct = ent.account_id if ent.account_id else income_account_id
        lines.append(
            {
                "account_id": cr_acct,
                "description": f"Entitlement: {ent.beneficiary_name} ({ent.percentage:.2f}%)",
                "debit": Decimal("0"),
                "credit": ent.amount,
            }
        )

    description = (
        f"Trust income distribution FY{dist.financial_year} "
        f"— {dist.distribution_date.isoformat()}"
    )
    entry = await journal_svc.create_draft(
        session,
        company_id=dist.company_id,
        tenant_id=dist.tenant_id,
        entry_date=dist.distribution_date,
        description=description,
        lines=lines,
    )
    posted = await journal_svc.post(
        session,
        entry.id,
        posted_by=posted_by,
        origin=JournalOrigin.TRUST_DISTRIBUTION,
        source_type="trust_distribution",
        source_id=dist.id,
    )

    dist.journal_entry_id = posted.id
    dist.status = DistributionStatus.POSTED
    await session.commit()
    await session.refresh(dist)
    return dist


async def delete(session: AsyncSession, distribution_id: uuid.UUID) -> None:
    dist = await session.get(TrustDistribution, distribution_id)
    if dist is None:
        return
    if dist.status == DistributionStatus.POSTED:
        raise DistributionError("Cannot delete a posted distribution — reverse the JE first.")
    dist.archived_at = datetime.now(UTC)
    await session.commit()
