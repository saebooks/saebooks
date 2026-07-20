"""Unit tests for the EE TSD Lisa 7 (dividends / equity payments)
serializer (pure — no DB).

Decent golden — header totals + 1b/1C/2/2B/4 row lists ARE populated in
the official example (build-plan §0.3); ``tsd_L7_3``/``tsd_L7_5`` are not
modelled (see generator.py's Lisa 7 section docstring).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.tsd import (
    TsdLisa7Header,
    TsdLisa7Listing,
    TsdLisa7Row1b,
    TsdLisa7Row1C,
    TsdLisa7Row2,
    TsdLisa7Row2B,
    TsdLisa7Row4,
    TsdReportingContext,
)
from saebooks.services.lodgement.tsd.serializer import build_tsd_lisa7_xml_document

_D = Decimal


def _ctx() -> TsdReportingContext:
    return TsdReportingContext(
        regcode="10123456", period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )


def _listing() -> TsdLisa7Listing:
    # Values lifted from the official populated example.
    return TsdLisa7Listing(
        header=TsdLisa7Header(dividends_total=_D("5000"), hidden_distributions=_D("1000")),
        rows_1b=[
            TsdLisa7Row1b(
                payer_regcode="1973378-1", payer_name="Swedbank Helsinki",
                payer_country_code="FI", income_type_code="701",
                payment_date=date(2025, 3, 20), foreign_income_amount=_D("300.55"),
                foreign_tax_paid=_D("40.35"), liability_reduction_amount=_D("40.35"),
            ),
        ],
        rows_1c=[TsdLisa7Row1C(year=2023, amount=_D("700"))],
        rows_2=[
            TsdLisa7Row2(
                payer_regcode="556016-0680", payer_name="Ericsson AB",
                payer_country_code="SE", income_type_code="730",
                payment_date=date(2025, 3, 21), dividend_participation_percent=None,
                equity_participation_percent=_D("15"), amount=_D("444"),
                foreign_tax_paid=_D("44"), foreign_taxed_profit=_D("55"),
                distributed_amount=_D("400"),
            ),
        ],
        rows_2b=[
            TsdLisa7Row2B(
                payer_regcode="10000704", payer_name='AKTSIASELTS & " NILES "',
                payer_country_code="EE", income_type_code="734",
                payment_date=date(2025, 3, 13), disguised_loan_amount=_D("900"),
                amount=_D("900"), month=1, year=2023, distributed_amount=_D("900"),
            ),
        ],
        rows_4=[
            TsdLisa7Row4(
                payer_regcode="71021997", payer_name='KOOPERATIIV "SUURKU"LA"',
                payer_country_code="EE", cooperative_social_tax=_D("100"),
                cooperative_foreign_tax_paid=_D("300"), cooperative_distributed=_D("400"),
                reduced_rate_dividends=_D("500"), tonnage_dividends=_D("600"),
            ),
        ],
    )


def test_lisa7_envelope_and_root_element() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    assert root.tag == "tsd_vorm"
    assert root.find("tsd_L7_0") is not None


def test_lisa7_header_totals() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    lisa7 = root.find("tsd_L7_0")
    assert lisa7.find("c7008_DivKokku").text == "5000.00"
    assert lisa7.find("c7012_VmKeSum").text == "1000.00"


def test_lisa7_row1b_foreign_tax_withheld_row() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L7_0").find("tsd_L7_1bList").findall("tsd_L7_1b")
    assert len(rows) == 1
    assert rows[0].find("c7101_Regkood").text == "1973378-1"
    assert rows[0].find("c7120_Kpv").text == "2025-03-20"   # xs:date, ISO format
    assert rows[0].find("c7130_VrSumma").text == "300.55"


def test_lisa7_row1c_prior_year_dividends_int_year() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L7_0").find("tsd_L7_1CList").findall("tsd_L7_1C")
    assert rows[0].find("c7020_Aasta").text == "2023"    # int, not "2023.00"


def test_lisa7_row2b_disguised_distribution_int_month_year() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L7_0").find("tsd_L7_2BList").findall("tsd_L7_2B")
    assert rows[0].find("c7213_Kuu").text == "1"
    assert rows[0].find("c7214_Aasta").text == "2023"


def test_lisa7_row4_cooperative_distribution() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(_listing(), _ctx()))
    rows = root.find("tsd_L7_0").find("tsd_L7_4List").findall("tsd_L7_4")
    assert rows[0].find("c7501_Regkood").text == "71021997"
    assert rows[0].find("c7510_AyhSm").text == "100.00"


def test_lisa7_empty_listing_omits_all_optional_lists() -> None:
    root = etree.fromstring(build_tsd_lisa7_xml_document(TsdLisa7Listing(), _ctx()))
    assert len(root.find("tsd_L7_0")) == 0
