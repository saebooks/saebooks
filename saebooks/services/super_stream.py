"""Payday Super lodgement aggregator + SAFF CSV generator.

Phase 1 scope: build a ``super_lodgement_runs`` row + per-employee
``super_lodgement_lines`` for a finalised pay run, render the SAFF v1
CSV the clearing-house portal accepts, and let the operator mark the
lodgement submitted once they've uploaded the file.

A "super lodgement" snapshots the employee + super-fund details as
they were on the pay run's payment_date. Member numbers, USIs, and
fund names can change between payday and the seven-day SuperStream
deadline; the lodgement record must reflect the state at the time the
contribution was earned, not the state when it was finally lodged.

References:
    - ATO SAFF v1 — Alternative File Format for SuperStream messages
      (~140 columns; this Phase 1 implementation produces the
      bookkeeper-facing subset the manual portal upload requires).
    - Treasury Laws Amendment (Payday Superannuation) Act 2025 —
      from 1 July 2026, SG must reach the fund within 7 days of pay.
"""
from __future__ import annotations

import csv
import logging
import os
import uuid
from io import StringIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

# Env-driven gate for Phase 1. Production stays OFF until Phase 2 lands the
# clearing-house API + ACK polling; dev/test/ci default ON so the workflow
# can be exercised end-to-end. The features.py tier matrix isn't the right
# home for this flag — payday-super isn't tier-gated, it's release-gated.
_FLAG_ENV_VAR = "SAEBOOKS_PAYDAY_SUPER"
_DEV_ENVS = frozenset({"dev", "development", "test", "ci"})


def is_payday_super_enabled() -> bool:
    """Return True when the Payday Super pay-run hook should fire.

    Resolution:
    * ``SAEBOOKS_PAYDAY_SUPER`` explicit override wins (``1`` / ``true`` /
      ``on`` enable; ``0`` / ``false`` / ``off`` disable).
    * Otherwise fall back to ``SAEBOOKS_ENV`` — on in dev/test/ci, off
      everywhere else.
    """
    override = os.environ.get(_FLAG_ENV_VAR, "").strip().lower()
    if override in {"1", "true", "on", "yes"}:
        return True
    if override in {"0", "false", "off", "no"}:
        return False
    env = os.environ.get("SAEBOOKS_ENV", "").strip().lower()
    return env in _DEV_ENVS


class SuperLodgementError(ValueError):
    """Domain-level validation failure on a super lodgement run."""


def _split_name(full_name: str | None) -> tuple[str | None, str]:
    """Split a single ``contacts.name`` field into (first, last).

    The SAFF v1 spec wants separate given-name + family-name columns.
    Internal Contact records carry a single ``name``. Pragmatic split:
    last token is the family name, everything before is the given name.
    Single-word names land entirely in ``last_name``.
    """
    if not full_name:
        return None, ""
    cleaned = full_name.strip()
    if not cleaned:
        return None, ""
    parts = cleaned.rsplit(" ", 1)
    if len(parts) == 1:
        return None, parts[0]
    return parts[0], parts[1]


async def build_super_lodgement_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID,
    notes: str | None = None,
) -> uuid.UUID:
    """Create (or replace DRAFT of) a super lodgement run for a pay run.

    If a non-DRAFT (FINALISED+) run already exists for this pay_run_id,
    raise — the caller must void it explicitly before regenerating.
    Existing DRAFTs are dropped and re-aggregated from scratch so the
    SAFF totals always match the pay-run-line state at the time the
    run is built.

    Returns the new super_lodgement_run id.
    """
    pay_run = (
        await session.execute(
            text(
                """
                SELECT id, company_id, tenant_id,
                       period_start, period_end, payment_date
                  FROM pay_runs
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                   AND archived_at IS NULL
                """
            ),
            {"id": str(pay_run_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if pay_run is None:
        raise SuperLodgementError(
            f"Pay run {pay_run_id} not found in company {company_id}"
        )

    existing = (
        await session.execute(
            text(
                """
                SELECT id, status FROM super_lodgement_runs
                 WHERE company_id = :c AND pay_run_id = :p AND tenant_id = :t
                """
            ),
            {"c": str(company_id), "p": str(pay_run_id), "t": str(tenant_id)},
        )
    ).first()
    if existing is not None:
        if existing[1] not in ("DRAFT", "VOIDED"):
            raise SuperLodgementError(
                f"Super lodgement for pay run {pay_run_id} already in "
                f"status {existing[1]} — void it before regenerating"
            )
        await session.execute(
            text("DELETE FROM super_lodgement_runs WHERE id = :id"),
            {"id": str(existing[0])},
        )

    new_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO super_lodgement_runs (
                id, company_id, tenant_id, pay_run_id,
                period_start, period_end, payment_date,
                status, notes
            ) VALUES (
                :id, :c, :t, :p,
                :ps, :pe, :pd,
                'DRAFT', :n
            )
            """
        ),
        {
            "id": str(new_id),
            "c": str(company_id),
            "t": str(tenant_id),
            "p": str(pay_run_id),
            "ps": pay_run[3],
            "pe": pay_run[4],
            "pd": pay_run[5],
            "n": notes,
        },
    )

    # Resolve the company-default super fund. Employees without an
    # explicit super_fund_id inherit it (the same fallback the pay-run
    # uses when posting the super-payable JE line).
    default_fund_row = (
        await session.execute(
            text(
                """
                SELECT id FROM super_funds
                 WHERE company_id = :c AND is_default = TRUE
                   AND archived_at IS NULL
                """
            ),
            {"c": str(company_id)},
        )
    ).first()
    default_fund_id = default_fund_row[0] if default_fund_row else None

    # One row per (pay-run-line, fund). pay_run_line.super_amount already
    # consolidates SG + salary-sacrifice + additional employer contribs
    # (the breakdown lives in payg_breakdown jsonb). Phase 1 reports the
    # whole super_amount as sg_amount; the SS / additional split lands in
    # Phase 2 when we wire the deduction/allowance categorisation through.
    await session.execute(
        text(
            """
            INSERT INTO super_lodgement_lines (
                id, super_lodgement_run_id, employee_id, super_fund_id,
                tenant_id,
                employee_first_name, employee_last_name,
                employee_tfn_status,
                employee_address_line1, employee_address_line2,
                employee_suburb, employee_state, employee_postcode,
                employee_email,
                fund_name, fund_usi, fund_spin,
                fund_is_smsf, fund_employer_abn, fund_esa,
                member_number,
                gross_payment, sg_amount,
                salary_sacrifice_amount, additional_amount,
                total_amount
            )
            SELECT
                gen_random_uuid(),
                :run_id,
                e.id,
                COALESCE(e.super_fund_id, CAST(:default_fund_id AS uuid)),
                CAST(:t AS uuid),
                -- pragmatic split: last token = family name; rest = given
                NULLIF(
                    regexp_replace(c.name, '\\s+[^\\s]+$', ''),
                    c.name
                ),
                COALESCE(
                    regexp_replace(c.name, '^.*\\s', ''),
                    c.name
                ),
                e.tfn_status,
                e.address_line1, e.address_line2,
                e.suburb, e.state, e.postcode,
                COALESCE(e.payslip_email, c.email),
                f.name, f.usi, f.spin,
                COALESCE(f.is_smsf, FALSE),
                f.employer_abn, f.esa,
                e.super_member_number,
                prl.gross,
                prl.super_amount,
                0, 0,
                prl.super_amount
            FROM pay_run_lines prl
            JOIN employees e ON e.id = prl.employee_id
            JOIN contacts  c ON c.id = e.contact_id
            LEFT JOIN super_funds f
                   ON f.id = COALESCE(e.super_fund_id, CAST(:default_fund_id AS uuid))
            WHERE prl.pay_run_id = :pay_run_id
              AND prl.super_amount > 0
            """
        ),
        {
            "run_id": str(new_id),
            "pay_run_id": str(pay_run_id),
            "default_fund_id": str(default_fund_id) if default_fund_id else None,
            "t": str(tenant_id),
        },
    )

    await session.execute(
        text(
            """
            UPDATE super_lodgement_runs r
               SET total_employee_count = sub.n,
                   total_amount = sub.t
              FROM (
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(total_amount), 0) AS t
                  FROM super_lodgement_lines
                 WHERE super_lodgement_run_id = :id
              ) sub
             WHERE r.id = :id
            """
        ),
        {"id": str(new_id)},
    )
    await session.commit()
    return new_id


async def finalise_super_lodgement_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    finalised_by: str,
) -> None:
    """Move a DRAFT run to FINALISED. Locks subsequent edits."""
    result = await session.execute(
        text(
            """
            UPDATE super_lodgement_runs
               SET status = 'FINALISED',
                   finalised_at = now(),
                   finalised_by = :by,
                   version = version + 1,
                   updated_at = now()
             WHERE id = :id
               AND tenant_id = :t
               AND status = 'DRAFT'
            RETURNING id
            """
        ),
        {"id": str(run_id), "t": str(tenant_id), "by": finalised_by},
    )
    if result.first() is None:
        raise SuperLodgementError(
            "Super lodgement run not found or not in DRAFT status"
        )
    await session.commit()


async def mark_super_lodgement_submitted(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    reference: str,
) -> None:
    """Move FINALISED → SUBMITTED with the clearing-house receipt id."""
    if not reference or not reference.strip():
        raise SuperLodgementError("Clearing-house reference is required")
    result = await session.execute(
        text(
            """
            UPDATE super_lodgement_runs
               SET status = 'SUBMITTED',
                   submitted_at = now(),
                   submitted_reference = :ref,
                   version = version + 1,
                   updated_at = now()
             WHERE id = :id
               AND tenant_id = :t
               AND status = 'FINALISED'
            RETURNING id
            """
        ),
        {"id": str(run_id), "t": str(tenant_id), "ref": reference.strip()[:128]},
    )
    if result.first() is None:
        raise SuperLodgementError(
            "Super lodgement run not found or not in FINALISED status"
        )
    await session.commit()


async def get_super_lodgement_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict | None:
    row = (
        await session.execute(
            text(
                """
                SELECT id, pay_run_id, period_start, period_end, payment_date,
                       status, generated_at,
                       finalised_at, finalised_by,
                       submitted_at, submitted_reference,
                       total_employee_count, total_amount,
                       notes, version
                  FROM super_lodgement_runs
                 WHERE id = :id AND company_id = :c AND tenant_id = :t
                """
            ),
            {"id": str(run_id), "c": str(company_id), "t": str(tenant_id)},
        )
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "pay_run_id": str(row[1]),
        "period_start": row[2].isoformat(),
        "period_end": row[3].isoformat(),
        "payment_date": row[4].isoformat(),
        "status": row[5],
        "generated_at": row[6].isoformat() if row[6] else None,
        "finalised_at": row[7].isoformat() if row[7] else None,
        "finalised_by": row[8],
        "submitted_at": row[9].isoformat() if row[9] else None,
        "submitted_reference": row[10],
        "total_employee_count": row[11],
        "total_amount": str(row[12]),
        "notes": row[13],
        "version": row[14],
    }


async def list_super_lodgement_runs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[dict]:
    sql = """
        SELECT id, pay_run_id, period_start, period_end, payment_date,
               status, total_employee_count, total_amount,
               generated_at, finalised_at, submitted_at, submitted_reference
          FROM super_lodgement_runs
         WHERE company_id = :c AND tenant_id = :t
           AND archived_at IS NULL
    """
    params: dict[str, object] = {"c": str(company_id), "t": str(tenant_id)}
    if pay_run_id is not None:
        sql += " AND pay_run_id = :p"
        params["p"] = str(pay_run_id)
    if status is not None:
        sql += " AND status = :s"
        params["s"] = status
    sql += " ORDER BY payment_date DESC, generated_at DESC"
    rows = (await session.execute(text(sql), params)).all()
    return [
        {
            "id": str(r[0]),
            "pay_run_id": str(r[1]),
            "period_start": r[2].isoformat(),
            "period_end": r[3].isoformat(),
            "payment_date": r[4].isoformat(),
            "status": r[5],
            "total_employee_count": r[6],
            "total_amount": str(r[7]),
            "generated_at": r[8].isoformat() if r[8] else None,
            "finalised_at": r[9].isoformat() if r[9] else None,
            "submitted_at": r[10].isoformat() if r[10] else None,
            "submitted_reference": r[11],
        }
        for r in rows
    ]


async def list_super_lodgement_lines(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT employee_id, super_fund_id,
                       employee_first_name, employee_last_name,
                       employee_tfn_status,
                       employee_address_line1, employee_address_line2,
                       employee_suburb, employee_state, employee_postcode,
                       employee_email,
                       fund_name, fund_usi, fund_spin,
                       fund_is_smsf, fund_employer_abn, fund_esa,
                       member_number,
                       gross_payment, sg_amount,
                       salary_sacrifice_amount, additional_amount,
                       total_amount
                  FROM super_lodgement_lines
                 WHERE super_lodgement_run_id = :id AND tenant_id = :t
                 ORDER BY employee_last_name, employee_first_name
                """
            ),
            {"id": str(run_id), "t": str(tenant_id)},
        )
    ).all()
    return [
        {
            "employee_id": str(r[0]),
            "super_fund_id": str(r[1]) if r[1] else None,
            "employee_first_name": r[2],
            "employee_last_name": r[3],
            "employee_tfn_status": r[4],
            "employee_address_line1": r[5],
            "employee_address_line2": r[6],
            "employee_suburb": r[7],
            "employee_state": r[8],
            "employee_postcode": r[9],
            "employee_email": r[10],
            "fund_name": r[11],
            "fund_usi": r[12],
            "fund_spin": r[13],
            "fund_is_smsf": r[14],
            "fund_employer_abn": r[15],
            "fund_esa": r[16],
            "member_number": r[17],
            "gross_payment": str(r[18]),
            "sg_amount": str(r[19]),
            "salary_sacrifice_amount": str(r[20]),
            "additional_amount": str(r[21]),
            "total_amount": str(r[22]),
        }
        for r in rows
    ]


# --------------------------------------------------------------------- #
# SAFF v1 CSV                                                           #
# --------------------------------------------------------------------- #

# SAFF v1, ATO-aligned. This column set tracks the bookkeeper-facing
# subset of the ~140-column SAFF specification — the fields required to
# satisfy a clearing-house portal upload for Phase 1 (manual lodgement).
# Re-verify against the current ATO schema before Phase 2 wires in
# direct clearing-house API submission.
#
# Source: https://www.ato.gov.au/businesses-and-organisations/super-for-employers/
#         superstream/superstream-for-employers/alternative-file-format-information
#
# Columns we deliberately leave blank with a TODO are flagged inline.
_SAFF_COLUMNS: tuple[str, ...] = (
    # --- Header / employer block --- #
    "Pay Period Start",
    "Pay Period End",
    "Pay Date",
    "Employer ABN",                                # TODO: pull from companies.abn
    "Employer Name",                               # TODO: pull from companies.name
    # --- Employee identity --- #
    "Employee Title",                              # TODO: not modelled (Mr/Ms/etc.)
    "Employee First Name",
    "Employee Other Given Names",                  # TODO: split second from first
    "Employee Family Name",
    "Employee Suffix",                             # TODO: not modelled
    "Employee Date of Birth",                      # TODO: requires Employee.dob
    "Employee Gender",                             # TODO: not modelled
    "Employee TFN",                                # TODO: encrypted; not exported in Phase 1
    "Employee TFN Status",
    "Employee Email",
    "Employee Phone",                              # TODO: not on Employee
    # --- Employee address --- #
    "Employee Address Line 1",
    "Employee Address Line 2",
    "Employee Suburb",
    "Employee State",
    "Employee Postcode",
    "Employee Country",                            # default AU
    # --- Fund identity --- #
    "Fund ABN",                                    # TODO: APRA fund ABN — not modelled
    "Fund USI",
    "Fund SPIN",
    "Fund Name",
    # --- SMSF-only --- #
    "SMSF Employer ABN",
    "SMSF ESA",
    "SMSF Bank BSB",                               # TODO: encrypted; SMSF-only
    "SMSF Bank Account Number",                    # TODO: encrypted; SMSF-only
    "SMSF Bank Account Name",                      # TODO: encrypted; SMSF-only
    # --- Membership --- #
    "Member Number",
    "Member Client Reference",                     # TODO: BMS-side member ref
    # --- Contributions --- #
    "Gross Payment",
    "SG Amount",
    "Salary Sacrifice Amount",
    "Additional Employer Amount",
    "Member Voluntary Amount",                     # TODO: post-tax member contrib
    "Spouse Contribution Amount",                  # TODO: not in scope
    "Child Contribution Amount",                   # TODO: not in scope
    "Total Contribution Amount",
)


def lines_to_saff_csv(run: dict, lines: list[dict]) -> bytes:
    """Render a SAFF v1 CSV for the given lodgement run + lines.

    Phase 1 caveats:

    * Employer ABN + name are emitted blank — caller fills the header
      in the portal (Phase 2 will resolve from ``companies`` via the
      service layer once the JE-finalise hook can join through).
    * TFN is intentionally NOT exported in plaintext. Phase 2 will
      gate decryption behind an explicit operator confirmation.
    * SMSF bank fields are blank for the same reason.
    * Columns marked with ``# TODO`` in ``_SAFF_COLUMNS`` are blank in
      Phase 1 — they require either model extensions (Employee.dob,
      gender, title, suffix) or downstream snapshots (APRA fund ABN
      lookup, member client reference). Document and revisit in Phase 2.
    """
    buf = StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(_SAFF_COLUMNS)

    period_start = run.get("period_start", "")
    period_end = run.get("period_end", "")
    payment_date = run.get("payment_date", "")

    for ln in lines:
        is_smsf = bool(ln.get("fund_is_smsf"))
        w.writerow([
            period_start,
            period_end,
            payment_date,
            "",                                  # Employer ABN — TODO
            "",                                  # Employer Name — TODO
            "",                                  # Title — TODO
            ln.get("employee_first_name") or "",
            "",                                  # Other given names — TODO
            ln.get("employee_last_name") or "",
            "",                                  # Suffix — TODO
            "",                                  # DOB — TODO
            "",                                  # Gender — TODO
            "",                                  # TFN — TODO (encrypted; Phase 2)
            ln.get("employee_tfn_status") or "",
            ln.get("employee_email") or "",
            "",                                  # Phone — TODO
            ln.get("employee_address_line1") or "",
            ln.get("employee_address_line2") or "",
            ln.get("employee_suburb") or "",
            ln.get("employee_state") or "",
            ln.get("employee_postcode") or "",
            "AU",
            "",                                  # Fund ABN — TODO (APRA lookup)
            ln.get("fund_usi") or "" if not is_smsf else "",
            ln.get("fund_spin") or "" if not is_smsf else "",
            ln.get("fund_name") or "",
            ln.get("fund_employer_abn") or "" if is_smsf else "",
            ln.get("fund_esa") or "" if is_smsf else "",
            "",                                  # SMSF BSB — TODO (encrypted)
            "",                                  # SMSF Acct # — TODO (encrypted)
            "",                                  # SMSF Acct Name — TODO (encrypted)
            ln.get("member_number") or "",
            "",                                  # Member client ref — TODO
            ln.get("gross_payment") or "0",
            ln.get("sg_amount") or "0",
            ln.get("salary_sacrifice_amount") or "0",
            ln.get("additional_amount") or "0",
            "",                                  # Member voluntary — TODO
            "",                                  # Spouse — TODO
            "",                                  # Child — TODO
            ln.get("total_amount") or "0",
        ])
    return buf.getvalue().encode("utf-8")


async def maybe_build_after_finalize(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    company_id: uuid.UUID,
    pay_run_id: uuid.UUID,
) -> uuid.UUID | None:
    """Best-effort hook after a pay-run finalise.

    Returns the lodgement-run id if one was built (or already existed
    and was DRAFT — re-aggregated), ``None`` if the flag is off, no
    super was due, or the build raised. **Never** re-raises: a failure
    here must not block the pay-run finalise (the JE has already been
    posted by the caller).
    """
    if not is_payday_super_enabled():
        return None
    try:
        has_super = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM pay_run_lines
                     WHERE pay_run_id = :p AND super_amount > 0
                     LIMIT 1
                    """
                ),
                {"p": str(pay_run_id)},
            )
        ).first()
        if has_super is None:
            return None
        return await build_super_lodgement_run(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            pay_run_id=pay_run_id,
        )
    except Exception:  # noqa: BLE001 — fire-and-forget on purpose
        _log.exception(
            "super lodgement build failed for pay_run %s; "
            "payroll finalise was NOT rolled back",
            pay_run_id,
        )
        return None


__all__ = [
    "SuperLodgementError",
    "is_payday_super_enabled",
    "build_super_lodgement_run",
    "finalise_super_lodgement_run",
    "mark_super_lodgement_submitted",
    "maybe_build_after_finalize",
    "get_super_lodgement_run",
    "list_super_lodgement_runs",
    "list_super_lodgement_lines",
    "lines_to_saff_csv",
]
