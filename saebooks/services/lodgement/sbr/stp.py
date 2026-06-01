"""STP2 PAYEVNT (Single Touch Payroll Phase 2) SBR document generator.

Maps the engine's STP2 payload (``services/stp.py:build_pay_event`` →
``StpSubmission.payload``, or an equivalent dict) onto an XBRL instance for the
lodge-server's ``/api/v1/stp/lodge`` route.

Shape
-----
STP2 is YTD-based: each payee reports year-to-date gross / PAYGW / super (the
ATO derives the period movement). The document is an employer header
(PAYEVNT — payer ABN/branch/name, payment date, software, totals) plus one
PAYEVNTEMP record per payee. In XBRL that's one **employer context** plus one
**context per payee** (all sharing the payer ABN entity + the report period),
with each payee's facts emitted under their own context.

⚠ CONFORMANCE STATUS — read before trusting the output
------------------------------------------------------
The field→figure MAPPING uses the real STP2 payload structure, but the
``_PAYEVNT_CONCEPTS`` / ``_PAYEVNTEMP_CONCEPTS`` element local-names, the
namespace and the schemaRef are **PLACEHOLDERS**. The authoritative PAYEVNT.0004
taxonomy concepts (and whether payees are modelled as XBRL tuples / typed
dimensions rather than per-payee contexts) come from the ATO STP2 MIG
(DSP-gated) and MUST be validated against the ATO EVTE before real lodgement.
The XBRL *structure* is sound; the concept *names* are not yet.

TFN: the engine payload carries ``tfn_status`` and an encrypted ref, NOT the
plaintext TFN (decryption is a lodge-server / submit-time step). This generator
emits ``tfn_status`` only; a real PAYEVNT injects the plaintext TFN at signing.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from saebooks.services.lodgement.sbr.xbrl import XbrlInstance

# ⚠ PLACEHOLDERS pending the ATO STP2 PAYEVNT.0004 taxonomy + MIG.
PAYEVNT_TAXONOMY_NS = "http://sbr.gov.au/PLACEHOLDER/ato/payevnt"  # TODO(MIG)
PAYEVNT_TAXONOMY_PREFIX = "payevnt"
PAYEVNT_SCHEMA_REF = "http://sbr.gov.au/PLACEHOLDER/ato/payevnt.xsd"  # TODO(MIG)

# Employer (PAYEVNT) level — TODO(MIG): replace local-names with taxonomy concepts.
_PAYEVNT_CONCEPTS: dict[str, str] = {
    "event_type": "PayEventType",
    "branch_code": "PayerBranchCode",
    "legal_name": "PayerOrganisationName",
    "payment_date": "PaymentDate",
    "software_name": "SoftwareName",
    "software_version": "SoftwareVersion",
    "payee_count": "PayeeRecordCount",
    "total_gross": "PayerTotalGrossPayments",
    "total_tax": "PayerTotalPaygWithholding",
    "total_super": "PayerTotalSuperLiability",
}

# Payee (PAYEVNTEMP) level — TODO(MIG).
_PAYEVNTEMP_CONCEPTS: dict[str, str] = {
    "payee_id_bms": "PayeeBmsIdentifier",
    "tfn_status": "PayeeTfnStatus",
    "name": "PayeeName",
    "dob": "PayeeDateOfBirth",
    "employment_basis": "EmploymentBasisCode",
    "tax_treatment_code": "TaxTreatmentCode",
    "income_stream_type": "IncomeStreamTypeCode",
    "ytd_gross": "PayeeYtdGrossPayments",
    "ytd_tax": "PayeeYtdPaygWithholding",
    "ytd_super": "PayeeYtdSuperLiability",
    "period_gross": "PayeePeriodGrossPayments",
    "period_tax": "PayeePeriodPaygWithholding",
    "period_super": "PayeePeriodSuperLiability",
    "super_usi": "SuperFundUsi",
    "super_member": "SuperMemberNumber",
}


def build_stp_pay_event_document(payload: dict[str, Any]) -> bytes:
    """Render an STP2 PAYEVNT as XBRL instance bytes from a build_pay_event payload.

    ``payload`` is the dict produced by ``services/stp.py:build_pay_event`` (also
    persisted as ``StpSubmission.payload``): keys ``employer``, ``report_period``,
    ``event_type``, ``submission_software``, ``payees`` (list), ``totals``.
    """
    employer = payload.get("employer") or {}
    abn = employer.get("abn") or ""
    period = payload.get("report_period") or {}
    start = date.fromisoformat(period["start"]) if period.get("start") else date.min
    end = date.fromisoformat(period["end"]) if period.get("end") else date.min

    inst = XbrlInstance(
        taxonomy_ns=PAYEVNT_TAXONOMY_NS,
        taxonomy_prefix=PAYEVNT_TAXONOMY_PREFIX,
        schema_ref=PAYEVNT_SCHEMA_REF,
    )

    # --- Employer (PAYEVNT) context + facts -------------------------------
    payer = inst.add_context("ctx-payer", abn=abn, period_start=start, period_end=end)
    software = payload.get("submission_software") or {}
    totals = payload.get("totals") or {}
    inst.add_text(_PAYEVNT_CONCEPTS["event_type"], payload.get("event_type"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["branch_code"], employer.get("branch_code"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["legal_name"], employer.get("legal_name"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["payment_date"], period.get("payment_date"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["software_name"], software.get("name"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["software_version"], software.get("version"), context_id=payer)
    inst.add_text(_PAYEVNT_CONCEPTS["payee_count"], totals.get("payee_count"), context_id=payer)
    inst.add_money(_PAYEVNT_CONCEPTS["total_gross"], totals.get("gross"), context_id=payer)
    inst.add_money(_PAYEVNT_CONCEPTS["total_tax"], totals.get("tax"), context_id=payer)
    inst.add_money(_PAYEVNT_CONCEPTS["total_super"], totals.get("super"), context_id=payer)

    # --- Per-payee (PAYEVNTEMP) contexts + facts --------------------------
    for index, payee in enumerate(payload.get("payees") or [], start=1):
        ctx = inst.add_context(
            f"ctx-payee-{index}", abn=abn, period_start=start, period_end=end
        )
        ytd = payee.get("ytd") or {}
        per = payee.get("period") or {}
        fund = payee.get("super_fund") or {}
        c = _PAYEVNTEMP_CONCEPTS
        inst.add_text(c["payee_id_bms"], payee.get("payee_id_bms"), context_id=ctx)
        inst.add_text(c["tfn_status"], payee.get("tfn_status"), context_id=ctx)
        inst.add_text(c["name"], payee.get("name"), context_id=ctx)
        inst.add_text(c["dob"], payee.get("dob"), context_id=ctx)
        inst.add_text(c["employment_basis"], payee.get("employment_basis"), context_id=ctx)
        inst.add_text(c["tax_treatment_code"], payee.get("tax_treatment_code"), context_id=ctx)
        inst.add_text(c["income_stream_type"], payee.get("income_stream_type"), context_id=ctx)
        inst.add_money(c["ytd_gross"], ytd.get("gross"), context_id=ctx)
        inst.add_money(c["ytd_tax"], ytd.get("tax"), context_id=ctx)
        inst.add_money(c["ytd_super"], ytd.get("super"), context_id=ctx)
        inst.add_money(c["period_gross"], per.get("gross"), context_id=ctx)
        inst.add_money(c["period_tax"], per.get("tax"), context_id=ctx)
        inst.add_money(c["period_super"], per.get("super"), context_id=ctx)
        inst.add_text(c["super_usi"], fund.get("usi"), context_id=ctx)
        inst.add_text(c["super_member"], fund.get("member_number"), context_id=ctx)

    return inst.to_bytes()
