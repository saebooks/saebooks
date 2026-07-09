"""Reference-DB SQLAlchemy models.

These map to ``saebooks_reference`` — a separate Postgres database on
the same cluster as ``saebooks_company_<uuid>``. There are NO foreign
keys from these tables to anything in the company DB; the boundary is
enforced by deploying them in different databases. Validation that a
companys ``tax_codes.code`` column resolves to a ``reference.tax_codes``
row happens at the service layer.

See docs/multi-jurisdiction.md for the broader design.
"""
from saebooks.models.reference.ato_interest_rate import AtoInterestRate
from saebooks.models.reference.benefit_in_kind_rate import (
    BENEFIT_IN_KIND_INCIDENCES,
    BENEFIT_IN_KIND_VALUATION_METHODS,
    BenefitInKindIncidence,
    BenefitInKindRate,
    BenefitInKindValuationMethod,
)
from saebooks.models.reference.bsb_directory import BsbDirectoryEntry
from saebooks.models.reference.capital_gains_tax_regime import (
    CGT_RELIEF_MECHANISMS,
    CapitalGainsTaxRegime,
    CgtReliefMechanism,
)
from saebooks.models.reference.chart_template import ChartTemplate
from saebooks.models.reference.corporate_tax_rate import CorporateTaxRate
from saebooks.models.reference.country import Country
from saebooks.models.reference.currency import Currency
from saebooks.models.reference.depreciation_effective_life import (
    DepreciationEffectiveLife,
)
from saebooks.models.reference.dividend_relief_mechanism import (
    DIVIDEND_RELIEF_MECHANISM_TYPES,
    DividendReliefMechanism,
    DividendReliefMechanismType,
)
from saebooks.models.reference.duty_concession import (
    DUTY_RELIEF_TYPES,
    DutyReliefType,
    RefDutyConcession,
)
from saebooks.models.reference.entity_structure import (
    ENTITY_STRUCTURE_BUCKETS,
    EntityStructureBucket,
    RefEntityStructureType,
)
from saebooks.models.reference.fbt_rate import FbtRate
from saebooks.models.reference.fiscal_year_definition import FiscalYearDefinition
from saebooks.models.reference.fuel_tax_credit_rate import FuelTaxCreditRate
from saebooks.models.reference.fx_rate_snapshot import RefFxRateSnapshot
from saebooks.models.reference.gst_registration_threshold import (
    GstRegistrationThreshold,
)
from saebooks.models.reference.holiday_calendar import HolidayCalendar
from saebooks.models.reference.income_tax_bracket import IncomeTaxBracket
from saebooks.models.reference.industry_code import IndustryCode
from saebooks.models.reference.jurisdiction import Jurisdiction
from saebooks.models.reference.mandatory_contribution_rule import (
    MandatoryContributionPayer,
    MandatoryContributionRule,
)
from saebooks.models.reference.medicare_levy import MedicareLevy
from saebooks.models.reference.payg_withholding_scale import PaygWithholdingScale
from saebooks.models.reference.payroll_tax_rate import PayrollTaxRate
from saebooks.models.reference.retirement_vehicle import (
    RETIREMENT_TAX_TREATMENTS,
    RETIREMENT_VEHICLE_BUCKETS,
    RefRetirementVehicleType,
    RetirementTaxTreatment,
    RetirementVehicleBucket,
)
from saebooks.models.reference.schema_meta import ReferenceSchemaMeta
from saebooks.models.reference.social_contribution_scheme import (
    CollectionMechanism,
    ContributionPayer,
    SocialContributionScheme,
)
from saebooks.models.reference.stamp_duty_rate import StampDutyRate
from saebooks.models.reference.super_contribution_cap import SuperContributionCap
from saebooks.models.reference.super_guarantee_rate import SuperGuaranteeRate
from saebooks.models.reference.tax_code import RefTaxCode, TaxDirection
from saebooks.models.reference.tax_id_validation_pattern import (
    TaxIdValidationPattern,
)
from saebooks.models.reference.tax_offset import TaxOffset
from saebooks.models.reference.tax_return_box_definition import (
    TaxReturnBoxDefinition,
)
from saebooks.models.reference.tax_rule import TaxRule
from saebooks.models.reference.withholding_table import (
    FormulaType,
    WithholdingTable,
    WithholdingType,
)

__all__ = [
    "BENEFIT_IN_KIND_INCIDENCES",
    "BENEFIT_IN_KIND_VALUATION_METHODS",
    "CGT_RELIEF_MECHANISMS",
    "DIVIDEND_RELIEF_MECHANISM_TYPES",
    "DUTY_RELIEF_TYPES",
    "ENTITY_STRUCTURE_BUCKETS",
    "RETIREMENT_TAX_TREATMENTS",
    "RETIREMENT_VEHICLE_BUCKETS",
    "AtoInterestRate",
    "BenefitInKindIncidence",
    "BenefitInKindRate",
    "BenefitInKindValuationMethod",
    "BsbDirectoryEntry",
    "CapitalGainsTaxRegime",
    "CgtReliefMechanism",
    "ChartTemplate",
    "CollectionMechanism",
    "ContributionPayer",
    "CorporateTaxRate",
    "Country",
    "Currency",
    "DepreciationEffectiveLife",
    "DividendReliefMechanism",
    "DividendReliefMechanismType",
    "DutyReliefType",
    "EntityStructureBucket",
    "FbtRate",
    "FiscalYearDefinition",
    "FormulaType",
    "FuelTaxCreditRate",
    "GstRegistrationThreshold",
    "HolidayCalendar",
    "IncomeTaxBracket",
    "IndustryCode",
    "Jurisdiction",
    "MandatoryContributionPayer",
    "MandatoryContributionRule",
    "MedicareLevy",
    "PaygWithholdingScale",
    "PayrollTaxRate",
    "RefDutyConcession",
    "RefEntityStructureType",
    "RefFxRateSnapshot",
    "RefRetirementVehicleType",
    "RefTaxCode",
    "ReferenceSchemaMeta",
    "RetirementTaxTreatment",
    "RetirementVehicleBucket",
    "SocialContributionScheme",
    "StampDutyRate",
    "SuperContributionCap",
    "SuperGuaranteeRate",
    "TaxDirection",
    "TaxIdValidationPattern",
    "TaxOffset",
    "TaxReturnBoxDefinition",
    "TaxRule",
    "WithholdingTable",
    "WithholdingType",
]
