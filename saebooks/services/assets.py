"""Fixed-asset register service.

Three flavours of public API:

1. **CRUD** — ``list_assets``, ``get``, ``create``, ``update``, ``archive``
   — straightforward per-company queries; no accounting side-effects.

2. **Depreciation** — ``compute_depreciation_through``,
   ``cumulative_depreciation_through``, ``post_depreciation``.

   Depreciation is generate-on-demand. We don't store a schedule table;
   we derive "amount owed through date X" from ``(cost, residual,
   in_service_date, method, method_number, method_period)`` and subtract
   what's already been posted (tracked by the ``last_depreciation_posted_through``
   date cursor on the asset row). Re-posting with the same ``through_date``
   is a guaranteed no-op.

3. **Disposal** — ``dispose_asset`` runs the closeout. It first brings
   depreciation up to the disposal date (so NBV is fresh), then posts
   a single journal:

       DR cash/bank (proceeds)
       DR accum_dep   (this asset's share of accumulated depreciation)
       CR cost        (full original cost — zeroes the asset line)
       CR gain  *or*  DR loss (the plug)

   NBV = ``cost - accum_dep`` at disposal date. Gain/loss = proceeds - NBV.

Linear depreciation math: day-count proration between ``in_service_date``
and ``in_service_date + useful_life`` (where useful life is
``method_number x method_period`` months, rendered as calendar days via
365.25/12 ≈ 30.4375 days per month). Caps at ``cost - residual``.

Diminishing-value (DV) math: rate is ``rate_pct`` (e.g. 30 for 30% per
year). We step month-by-month from ``in_service_date`` — each full
month's charge is ``book_value * rate / 12``. The first partial month
(and the month containing ``through``) are day-count-prorated inside
that month. Caps at ``cost - residual`` so book value asymptotes to
residual without ever undershooting (classical DV would never reach
zero otherwise).

No-depreciation ("asset_no_depreciation") models always yield 0 — safe
to post on, safe to call the depreciation functions on.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.account import Account
from saebooks.models.depreciation_model import DepreciationModel
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import journal as journal_svc

# Days-per-month used for day-count depreciation proration.
# 365.25 / 12 — handles leap years smoothly across long useful lives.
_DAYS_PER_MONTH = Decimal("30.4375")

_CENT = Decimal("0.01")


# ---------------------------------------------------------------------- #
# CRUD                                                                   #
# ---------------------------------------------------------------------- #


async def list_assets(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: str | None = "active",
    include_archived: bool = False,
    limit: int = 200,
) -> list[FixedAsset]:
    """List assets for a company.

    Defaults to ``status='active'`` (unarchived). Pass ``status=None``
    to return every status, or set ``include_archived=True`` to include
    soft-deleted rows.
    """
    stmt = select(FixedAsset).where(FixedAsset.company_id == company_id)
    if not include_archived:
        stmt = stmt.where(FixedAsset.archived_at.is_(None))
    if status is not None:
        stmt = stmt.where(FixedAsset.status == status)
    stmt = stmt.order_by(FixedAsset.code).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, asset_id: uuid.UUID) -> FixedAsset | None:
    return await session.get(FixedAsset, asset_id)


async def _next_code(session: AsyncSession, company_id: uuid.UUID) -> str:
    """Auto-generate the next ``FA-NNNN`` code for this company."""
    result = await session.execute(
        select(func.count()).where(FixedAsset.company_id == company_id)
    )
    count = result.scalar_one()
    return f"FA-{count + 1:04d}"


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    name: str,
    cost_account_id: uuid.UUID,
    accum_dep_account_id: uuid.UUID,
    dep_expense_account_id: uuid.UUID,
    depreciation_model_id: str,
    purchase_date: date,
    cost: Decimal,
    in_service_date: date | None = None,
    residual_value: Decimal | None = None,
    code: str | None = None,
    description: str | None = None,
    serial_number: str | None = None,
    manufacturer: str | None = None,
    model_number: str | None = None,
    location: str | None = None,
    custody_person: str | None = None,
    warranty_end: date | None = None,
    purchase_contact_id: uuid.UUID | None = None,
) -> FixedAsset:
    """Create a new fixed asset.

    ``code`` auto-generates to ``FA-NNNN`` if not supplied.
    ``in_service_date`` defaults to ``purchase_date``.
    ``residual_value`` defaults to 0.
    """
    asset = FixedAsset(
        company_id=company_id,
        code=code or await _next_code(session, company_id),
        name=name.strip(),
        description=description,
        cost_account_id=cost_account_id,
        accum_dep_account_id=accum_dep_account_id,
        dep_expense_account_id=dep_expense_account_id,
        depreciation_model_id=depreciation_model_id,
        purchase_date=purchase_date,
        in_service_date=in_service_date or purchase_date,
        cost=Decimal(cost).quantize(_CENT),
        residual_value=(
            Decimal(residual_value).quantize(_CENT)
            if residual_value is not None
            else Decimal("0")
        ),
        serial_number=serial_number,
        manufacturer=manufacturer,
        model_number=model_number,
        location=location,
        custody_person=custody_person,
        warranty_end=warranty_end,
        purchase_contact_id=purchase_contact_id,
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset


async def update(
    session: AsyncSession,
    asset_id: uuid.UUID,
    **fields: object,
) -> FixedAsset:
    """Update an active asset's editable fields.

    Refuses to edit a disposed asset — disposal is a terminal state.
    Money and dates that drive depreciation (``cost``, ``residual_value``,
    ``in_service_date``, ``depreciation_model_id``) may be edited only
    while no depreciation has been posted yet; once the clock's started,
    the history becomes immutable.
    """
    asset = await get(session, asset_id)
    if asset is None:
        raise ValueError(f"Fixed asset {asset_id} not found")
    if asset.status == "disposed":
        raise ValueError("Cannot edit a disposed asset")

    locked_once_posted = {
        "cost",
        "residual_value",
        "in_service_date",
        "depreciation_model_id",
        "cost_account_id",
        "accum_dep_account_id",
        "dep_expense_account_id",
    }
    if asset.last_depreciation_posted_through is not None:
        for key in fields:
            if key in locked_once_posted:
                raise ValueError(
                    f"Cannot edit {key!r} after depreciation has been posted"
                )

    for key, value in fields.items():
        if not hasattr(asset, key):
            raise ValueError(f"Unknown field {key!r}")
        setattr(asset, key, value)

    await session.commit()
    await session.refresh(asset)
    return asset


async def archive(session: AsyncSession, asset_id: uuid.UUID) -> FixedAsset:
    """Soft-delete by stamping ``archived_at``. Journal trail is kept."""
    asset = await get(session, asset_id)
    if asset is None:
        raise ValueError(f"Fixed asset {asset_id} not found")
    asset.archived_at = datetime.now(UTC)
    asset.status = "archived"
    await session.commit()
    await session.refresh(asset)
    return asset


# ---------------------------------------------------------------------- #
# Depreciation math                                                      #
# ---------------------------------------------------------------------- #


async def _load_model(
    session: AsyncSession, model_id: str
) -> DepreciationModel:
    model = await session.get(DepreciationModel, model_id)
    if model is None:
        raise ValueError(f"Depreciation model {model_id!r} not found")
    return model


def _useful_life_days(model: DepreciationModel) -> Decimal:
    """Calendar days of useful life for this model, using 30.4375 days/month."""
    months = Decimal(model.method_number) * Decimal(model.method_period)
    return months * _DAYS_PER_MONTH


def _cumulative_linear(
    *,
    depreciable_base: Decimal,
    in_service_date: date,
    useful_life_days: Decimal,
    through: date,
) -> Decimal:
    """Straight-line depreciation accumulated from ``in_service_date`` through ``through``.

    Day-count proration: the first day counts as day 1, so a same-day
    ``through`` yields 1/total_days of the base. Caps at the full
    depreciable base (cost - residual).
    """
    if through < in_service_date:
        return Decimal("0")
    if useful_life_days <= 0:
        return Decimal("0")
    elapsed_days = Decimal((through - in_service_date).days + 1)
    if elapsed_days >= useful_life_days:
        return depreciable_base.quantize(_CENT)
    return (depreciable_base * elapsed_days / useful_life_days).quantize(_CENT)


def _month_end(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def _cumulative_dv(
    *,
    cost: Decimal,
    residual: Decimal,
    in_service_date: date,
    annual_rate_pct: Decimal,
    through: date,
) -> Decimal:
    """Total diminishing-value depreciation from in_service through date.

    Walks month-by-month: each month charges
    ``book_value * (rate_pct / 100) / 12 * month_fraction`` where
    ``book_value = cost - accumulated_dep_so_far``. The first month
    is prorated by the day ``in_service_date`` lands on within that
    month; the final month is prorated by how far through the month
    ``through`` is. Book value floored at ``residual`` — we never
    depreciate below salvage.

    Cap stops the classical DV asymptote from overshooting. This is a
    pure function so tests can exercise the math without a DB.
    """
    if through < in_service_date:
        return Decimal("0")
    depreciable_base = cost - residual
    if depreciable_base <= 0:
        return Decimal("0")

    monthly_rate = annual_rate_pct / Decimal("100") / Decimal("12")
    accum = Decimal("0")

    cursor_year = in_service_date.year
    cursor_month = in_service_date.month
    first = True
    while True:
        m_start = date(cursor_year, cursor_month, 1)
        m_end = _month_end(cursor_year, cursor_month)
        if m_start > through:
            break

        # Fraction of this month that's active:
        #   · bounded below by the in-service day on the first month
        #   · bounded above by ``through`` on the closing month
        start_anchor = in_service_date if first else m_start
        end_anchor = through if through < m_end else m_end
        total_days_in_month = Decimal((m_end - m_start).days + 1)
        active_days = Decimal((end_anchor - start_anchor).days + 1)
        if active_days <= 0:
            break
        month_fraction = active_days / total_days_in_month

        book_value = cost - accum
        remaining_to_residual = book_value - residual
        if remaining_to_residual <= 0:
            break
        month_charge = (book_value * monthly_rate * month_fraction).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        if month_charge > remaining_to_residual:
            month_charge = remaining_to_residual.quantize(_CENT)
        accum += month_charge

        if end_anchor == through:
            break
        first = False
        if cursor_month == 12:
            cursor_month = 1
            cursor_year += 1
        else:
            cursor_month += 1

    return accum.quantize(_CENT)


async def cumulative_depreciation_through(
    session: AsyncSession,
    asset: FixedAsset,
    through: date,
) -> Decimal:
    """Total depreciation that should have accumulated by ``through``.

    Method-aware. Returns 0 for ``no_depreciation``. Capped at
    ``cost - residual``. Does NOT reference what's actually been posted
    — that's ``compute_depreciation_through``'s job.
    """
    model = await _load_model(session, asset.depreciation_model_id)

    if model.method == "no_depreciation":
        return Decimal("0")

    if model.method == "linear":
        depreciable_base = asset.cost - asset.residual_value
        if depreciable_base <= 0:
            return Decimal("0")
        return _cumulative_linear(
            depreciable_base=depreciable_base,
            in_service_date=asset.in_service_date,
            useful_life_days=_useful_life_days(model),
            through=through,
        )

    if model.method == "diminishing_value":
        if model.rate_pct is None or model.rate_pct <= 0:
            raise ValueError(
                f"DV model {model.id!r} missing positive rate_pct — "
                f"cannot compute depreciation"
            )
        depreciable_base = asset.cost - asset.residual_value
        if depreciable_base <= 0:
            return Decimal("0")
        return _cumulative_dv(
            cost=asset.cost,
            residual=asset.residual_value,
            in_service_date=asset.in_service_date,
            annual_rate_pct=model.rate_pct,
            through=through,
        )

    raise ValueError(
        f"Depreciation method {model.method!r} is not implemented — "
        f"add a handler in saebooks.services.assets"
    )


async def compute_depreciation_through(
    session: AsyncSession,
    asset: FixedAsset,
    through: date,
) -> Decimal:
    """Incremental amount owed between the cursor and ``through``.

    Returns 0 when ``through <= last_depreciation_posted_through`` —
    which makes ``post_depreciation`` idempotent on re-runs.
    """
    if (
        asset.last_depreciation_posted_through is not None
        and through <= asset.last_depreciation_posted_through
    ):
        return Decimal("0")

    total_through = await cumulative_depreciation_through(session, asset, through)
    if asset.last_depreciation_posted_through is None:
        already_posted = Decimal("0")
    else:
        already_posted = await cumulative_depreciation_through(
            session, asset, asset.last_depreciation_posted_through
        )
    delta = total_through - already_posted
    if delta < 0:
        return Decimal("0")
    return delta.quantize(_CENT)


# ---------------------------------------------------------------------- #
# Posting                                                                #
# ---------------------------------------------------------------------- #


async def post_depreciation(
    session: AsyncSession,
    asset_id: uuid.UUID,
    through: date,
    *,
    posted_by: str | None = None,
) -> tuple[FixedAsset, Decimal]:
    """Post depreciation from the cursor up to ``through``.

    Returns the refreshed asset and the amount posted. A zero-delta
    call posts no journal and is a no-op (returns ``(asset, 0)``).
    Advances ``last_depreciation_posted_through`` on success.
    """
    asset = await get(session, asset_id)
    if asset is None:
        raise ValueError(f"Fixed asset {asset_id} not found")
    if asset.status != "active":
        raise ValueError(
            f"Cannot depreciate asset in status {asset.status!r} — must be active"
        )

    amount = await compute_depreciation_through(session, asset, through)
    if amount == 0:
        # Still advance the cursor so the next run starts from ``through``.
        if (
            asset.last_depreciation_posted_through is None
            or through > asset.last_depreciation_posted_through
        ):
            asset.last_depreciation_posted_through = through
            await session.commit()
            await session.refresh(asset)
        return asset, Decimal("0")

    entry = await journal_svc.create_draft(
        session,
        company_id=asset.company_id,
        entry_date=through,
        description=f"Depreciation: {asset.code} {asset.name} through {through}",
        lines=[
            {
                "account_id": asset.dep_expense_account_id,
                "description": f"Depreciation {asset.code}",
                "debit": amount,
                "credit": Decimal("0"),
            },
            {
                "account_id": asset.accum_dep_account_id,
                "description": f"Accum dep {asset.code}",
                "debit": Decimal("0"),
                "credit": amount,
            },
        ],
    )
    await journal_svc.post(session, entry.id, posted_by=posted_by)

    asset.last_depreciation_posted_through = through
    await session.commit()
    await session.refresh(asset)
    return asset, amount


# ---------------------------------------------------------------------- #
# Disposal                                                               #
# ---------------------------------------------------------------------- #


async def _find_account_by_code(
    session: AsyncSession, company_id: uuid.UUID, code: str
) -> Account:
    result = await session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code == code,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise ValueError(
            f"Account {code!r} not found for company — seed may be incomplete"
        )
    return account


async def dispose_asset(
    session: AsyncSession,
    asset_id: uuid.UUID,
    *,
    disposal_date: date,
    proceeds: Decimal,
    cash_account_id: uuid.UUID,
    posted_by: str | None = None,
) -> tuple[FixedAsset, Decimal]:
    """Dispose of an asset.

    1. Catches depreciation up to ``disposal_date`` if needed.
    2. Posts a single disposal journal:

       - DR cash account (proceeds)
       - DR accum_dep account (NBV offset — clears this asset's share)
       - CR cost account (full cost — clears the original capitalisation)
       - CR gain (``4-9100``) if ``proceeds > NBV``
       - DR loss (``6-9100``) if ``proceeds < NBV``
       - Perfectly balanced entry if ``proceeds == NBV``

    Marks the asset ``disposed``, stamps ``disposal_date``, ``disposal_proceeds``,
    and ``disposal_journal_id``. Returns ``(asset, gain_loss)`` where
    positive = gain, negative = loss.
    """
    asset = await get(session, asset_id)
    if asset is None:
        raise ValueError(f"Fixed asset {asset_id} not found")
    if asset.status != "active":
        raise ValueError(
            f"Cannot dispose asset in status {asset.status!r} — must be active"
        )

    proceeds = Decimal(proceeds).quantize(_CENT)

    # Step 1: catch depreciation up to disposal_date so NBV is current.
    await post_depreciation(
        session, asset_id, disposal_date, posted_by=posted_by
    )
    # Reload to get the fresh cursor.
    asset = await get(session, asset_id)
    assert asset is not None  # reload — same PK, cannot disappear

    # Step 2: compute NBV using the same formula we used to post dep.
    accum_dep = await cumulative_depreciation_through(
        session, asset, disposal_date
    )
    nbv = (asset.cost - accum_dep).quantize(_CENT)
    gain_loss = (proceeds - nbv).quantize(_CENT)

    # Step 3: build journal lines.
    lines: list[dict[str, object]] = [
        {
            "account_id": cash_account_id,
            "description": f"Proceeds from disposal of {asset.code}",
            "debit": proceeds,
            "credit": Decimal("0"),
        },
    ]
    if accum_dep > 0:
        lines.append(
            {
                "account_id": asset.accum_dep_account_id,
                "description": f"Clear accum dep for {asset.code}",
                "debit": accum_dep,
                "credit": Decimal("0"),
            }
        )
    lines.append(
        {
            "account_id": asset.cost_account_id,
            "description": f"Clear cost of {asset.code}",
            "debit": Decimal("0"),
            "credit": asset.cost,
        }
    )
    if gain_loss > 0:
        gain_acct = await _find_account_by_code(
            session, asset.company_id, "4-9100"
        )
        lines.append(
            {
                "account_id": gain_acct.id,
                "description": f"Gain on disposal of {asset.code}",
                "debit": Decimal("0"),
                "credit": gain_loss,
            }
        )
    elif gain_loss < 0:
        loss_acct = await _find_account_by_code(
            session, asset.company_id, "6-9100"
        )
        lines.append(
            {
                "account_id": loss_acct.id,
                "description": f"Loss on disposal of {asset.code}",
                "debit": -gain_loss,
                "credit": Decimal("0"),
            }
        )

    entry = await journal_svc.create_draft(
        session,
        company_id=asset.company_id,
        entry_date=disposal_date,
        description=f"Disposal: {asset.code} {asset.name}",
        lines=lines,
    )
    posted = await journal_svc.post(session, entry.id, posted_by=posted_by)

    # Step 4: stamp asset with disposal metadata.
    asset.status = "disposed"
    asset.disposal_date = disposal_date
    asset.disposal_proceeds = proceeds
    asset.disposal_journal_id = posted.id
    await session.commit()
    await session.refresh(asset)
    return asset, gain_loss


# ---------------------------------------------------------------------- #
# Partial disposal (Batch MM/3)                                          #
# ---------------------------------------------------------------------- #


async def dispose_partial(
    session: AsyncSession,
    asset_id: uuid.UUID,
    *,
    fraction: Decimal,
    disposal_date: date,
    proceeds: Decimal,
    cash_account_id: uuid.UUID,
    posted_by: str | None = None,
) -> tuple[FixedAsset, FixedAsset, Decimal]:
    """Dispose a fraction (0 < f < 1) of an active asset.

    Splits the original row:

    * **Parent** (the row pointed at by ``asset_id``) has its ``cost``
      and ``residual_value`` reduced by the disposed share. Remains
      ``active`` — future depreciation naturally computes on the
      reduced cost from ``disposal_date`` onward.
    * **Child** is a fresh ``FixedAsset`` row pinned to the parent via
      ``parent_asset_id``. It carries the disposed share of cost and
      residual, uses the same accounts + model, inherits
      ``in_service_date`` + ``purchase_date`` from the parent, and
      has its ``last_depreciation_posted_through`` fast-forwarded to
      ``disposal_date`` so the downstream ``dispose_asset`` call
      short-circuits the catch-up pass.

    ``dispose_asset`` then runs against the child — posting the usual
    DR cash / DR accum_dep / CR cost / plug-gain-or-loss journal on
    just the disposed fraction. The parent's cost and accum_dep are
    untouched on the GL (the share that stays with the parent).

    Returns ``(parent, child, gain_loss)`` — same gain_loss convention
    as ``dispose_asset`` (positive = gain, negative = loss).

    Validation:

    * ``0 < fraction < 1`` — use ``dispose_asset`` for a full disposal.
    * Asset must be ``active``.
    * Depreciation is caught up on the parent before the split so the
      shares are computed against a current NBV.
    """
    fraction = Decimal(fraction)
    if fraction <= 0 or fraction >= 1:
        raise ValueError(
            f"fraction must be in (0, 1); got {fraction} — "
            "use dispose_asset() for full disposal"
        )

    asset = await get(session, asset_id)
    if asset is None:
        raise ValueError(f"Fixed asset {asset_id} not found")
    if asset.status != "active":
        raise ValueError(
            f"Cannot partially dispose asset in status {asset.status!r} "
            "— must be active"
        )

    # Step 1: bring depreciation current on the parent so shares are
    # computed against the right NBV.
    await post_depreciation(
        session, asset_id, disposal_date, posted_by=posted_by
    )
    asset = await get(session, asset_id)
    assert asset is not None  # reload — same PK, cannot disappear

    # Step 2: compute the disposed-fraction shares. Round both to the
    # cent so the remainder that stays with the parent is an exact
    # subtraction (cent-balanced).
    child_cost = (asset.cost * fraction).quantize(_CENT)
    child_residual = (asset.residual_value * fraction).quantize(_CENT)
    if child_cost <= 0:
        raise ValueError(
            f"fraction {fraction} too small — disposed cost rounds to zero"
        )
    parent_new_cost = (asset.cost - child_cost).quantize(_CENT)
    parent_new_residual = (
        asset.residual_value - child_residual
    ).quantize(_CENT)

    # Step 3: create the child row. Copies GL coords + model + dates
    # from the parent; the dep cursor fast-forwards to disposal_date
    # so dispose_asset's internal post_depreciation catch-up is a no-op.
    child_code = f"{asset.code}-P{datetime.now(UTC).strftime('%H%M%S')}"
    child = FixedAsset(
        company_id=asset.company_id,
        code=child_code,
        name=f"{asset.name} (disposed portion)",
        description=asset.description,
        cost_account_id=asset.cost_account_id,
        accum_dep_account_id=asset.accum_dep_account_id,
        dep_expense_account_id=asset.dep_expense_account_id,
        depreciation_model_id=asset.depreciation_model_id,
        purchase_date=asset.purchase_date,
        in_service_date=asset.in_service_date,
        cost=child_cost,
        residual_value=child_residual,
        last_depreciation_posted_through=disposal_date,
        parent_asset_id=asset.id,
        status="active",
    )
    session.add(child)
    await session.flush()  # get child.id for the dispose call

    # Step 4: reduce parent's cost + residual by the disposed share.
    asset.cost = parent_new_cost
    asset.residual_value = parent_new_residual
    await session.commit()
    await session.refresh(asset)
    await session.refresh(child)

    # Step 5: dispose the child — posts the closeout journal for just
    # the disposed share.
    disposed_child, gain_loss = await dispose_asset(
        session,
        child.id,
        disposal_date=disposal_date,
        proceeds=proceeds,
        cash_account_id=cash_account_id,
        posted_by=posted_by,
    )

    # Reload parent one more time — the dispose_asset call may have
    # expired the session's view of it.
    await session.refresh(asset)
    return asset, disposed_child, gain_loss
