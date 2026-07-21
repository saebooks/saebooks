"""Tests for the TPAR BDE (FPAIVV03.0) flat-file writer.

Pure serialisation tests — no database. Field positions and sample
values come straight from the archived ATO spec v3.0.1
(``~/records/saebooks/ato-artefacts/tpar-bde-spec-v301/``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from saebooks.jurisdictions.au.bde.tpar import (
    RECORD_LENGTH,
    BdeAddress,
    BdePayee,
    BdePayer,
    BdeSender,
    TparBdeError,
    build_tpar_bde_file,
)

VALID_ABN = "51 824 753 556"  # passes the mod-89 checksum

ADDRESS = BdeAddress(
    line1="10 First Street",
    suburb="Brisbane",
    state="QLD",
    postcode="4000",
)

SENDER = BdeSender(
    abn=VALID_ABN,
    name="The Trustee for Example Trust",
    contact_name="R Example",
    phone="0733331234",
    address=ADDRESS,
    email="admin@example.com.au",
)

PAYER = BdePayer(
    abn=VALID_ABN,
    financial_year=2026,
    name="The Trustee for Example Trust",
    address=ADDRESS,
    contact_name="R Example",
    phone="0733331234",
)


def _payee(**overrides) -> BdePayee:
    fields: dict = dict(
        address=ADDRESS,
        gross=Decimal("24120.50"),
        tax_withheld=Decimal("1698.99"),
        gst=Decimal("964.10"),
        abn=VALID_ABN,
        business_name="Concrete Pty Ltd",
    )
    fields.update(overrides)
    return BdePayee(**fields)


def _records(payees: list[BdePayee], **kwargs) -> list[str]:
    raw = build_tpar_bde_file(
        SENDER, PAYER, payees, software_developer="Example Trust", **kwargs
    ).decode("ascii")
    return [raw[i : i + RECORD_LENGTH] for i in range(0, len(raw), RECORD_LENGTH)]


def test_file_structure_and_total() -> None:
    records = _records([_payee(), _payee(business_name="Sparks Pty Ltd")])
    # 3 sender headers + IDENTITY + SOFTWARE + 2 payees + FILE-TOTAL
    assert len(records) == 8
    assert all(len(r) == RECORD_LENGTH for r in records)
    assert [r[3:17].strip() for r in records[:3]] == [
        "IDENTREGISTER1", "IDENTREGISTER2", "IDENTREGISTER3",
    ]
    assert records[3][3:11] == "IDENTITY"
    assert records[4][3:11] == "SOFTWARE"
    assert records[5][3:9] == "DPAIVS"
    # FILE-TOTAL count includes every record, itself included (6.72).
    assert records[-1][3:13] == "FILE-TOTAL"
    assert records[-1][13:21] == "00000008"


def test_identregister1_fields() -> None:
    r1 = _records([_payee()])[0]
    assert r1[0:3] == "996"
    assert r1[17:28] == "51824753556"
    assert r1[28] == "P"  # run type defaults to production
    assert r1[29:37] == "30062026"  # report end date = FY end
    assert r1[37:40] == "PCM"  # data type, type of report, return media
    assert r1[40:50] == "FPAIVV03.0"
    assert r1[50:].strip() == ""  # filler blank to 996


def test_run_type_test_flag() -> None:
    r1 = _records([_payee()], run_type="T")[0]
    assert r1[28] == "T"


def test_software_record_is_inhouse() -> None:
    sw = _records([_payee()])[4]
    assert sw[11:91].rstrip() == "INHOUSE Example Trust"


def test_payee_amounts_truncate_cents_at_spec_positions() -> None:
    p = _records([_payee()])[5]
    # Spec example values: whole dollars, right-justified, zero-filled.
    assert p[640:651] == "00000024120"  # gross 24120.50
    assert p[651:662] == "00000001698"  # withheld 1698.99
    assert p[662:673] == "00000000964"  # GST 964.10
    assert p[673] == "P"
    assert p[674:682] == "00000000"  # no grant date
    assert p[958] == "N"  # statement by supplier
    assert p[959] == "O"  # original, not amendment
    assert p[960] == " "  # NANE not reported


def test_payee_abn_zero_filled_when_not_quoted() -> None:
    p = _records([_payee(abn="", statement_by_supplier=True)])[5]
    assert p[9:20] == "0" * 11
    assert p[958] == "Y"


def test_individual_payee_name_split() -> None:
    p = _records(
        [_payee(business_name="", family_name="Smith", given_name="Alex")]
    )[5]
    assert p[20:50].rstrip() == "Smith"
    assert p[50:65].rstrip() == "Alex"
    assert p[80:280].strip() == ""  # business name blank for individuals


def test_payee_needs_some_name() -> None:
    with pytest.raises(TparBdeError, match="business name or a family name"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee(business_name="")],
            software_developer="Example Trust",
        )


def test_overseas_payee_shape() -> None:
    overseas = BdeAddress(
        line1="1 Vabaduse valjak", suburb="Tallinn",
        state="OTH", postcode="9999", country="Estonia",
    )
    p = _records([_payee(address=overseas)])[5]
    assert p[583:586] == "OTH"
    assert p[586:590] == "9999"
    assert p[590:610].rstrip() == "Estonia"


def test_overseas_without_country_rejected() -> None:
    bad = BdeAddress(line1="1 Somewhere", suburb="Town", state="OTH", postcode="9999")
    with pytest.raises(TparBdeError, match="no country"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee(address=bad)], software_developer="X"
        )


def test_invalid_state_rejected() -> None:
    bad = BdeAddress(line1="1 Street", suburb="Town", state="XX", postcode="4000")
    with pytest.raises(TparBdeError, match="state"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee(address=bad)], software_developer="X"
        )


def test_invalid_abn_checksum_rejected() -> None:
    with pytest.raises(TparBdeError, match="checksum"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee(abn="51824753557")], software_developer="X"
        )


def test_zero_gross_rejected() -> None:
    with pytest.raises(TparBdeError, match="greater than zero"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee(gross=0)], software_developer="X"
        )


def test_formatted_abn_and_bsb_normalised() -> None:
    p = _records([_payee(bsb="064-000", account_number="12345678")])[5]
    assert p[9:20] == "51824753556"
    assert p[625:631] == "064000"
    assert p[631:640] == "012345678"


def test_crlf_mode_terminates_every_record() -> None:
    raw = build_tpar_bde_file(
        SENDER, PAYER, [_payee()], software_developer="X", crlf=True
    ).decode("ascii")
    lines = raw.split("\r\n")
    assert lines[-1] == ""  # trailing pair on the last record
    assert all(len(line) == RECORD_LENGTH for line in lines[:-1])


def test_text_transliterated_to_ascii() -> None:
    p = _records([_payee(business_name="Pärnu Tänav OÜ")])[5]
    assert p[80:280].rstrip() == "Parnu Tanav OU"


def test_report_end_date_must_sit_in_fy() -> None:
    with pytest.raises(TparBdeError, match="outside FY"):
        build_tpar_bde_file(
            SENDER, PAYER, [_payee()], software_developer="X",
            report_end_date=date(2024, 6, 30),
        )


# ------------------------------------------------------------------ #
# End-to-end: ledger → tpar_run → BDE file (postgres only)            #
# ------------------------------------------------------------------ #


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_tpar_run_renders_bde_file() -> None:
    """A posted bill to a TPAR-flagged individual flows through
    build_tpar_run's snapshot columns into a valid FPAIVV03.0 file."""
    import secrets

    from sqlalchemy import select

    from saebooks.db import AsyncSessionLocal
    from saebooks.jurisdictions.au import tpar as svc
    from saebooks.models.account import Account
    from saebooks.models.company import Company
    from saebooks.models.contact import Contact, ContactType
    from saebooks.services import bills as bill_svc
    from saebooks.services import business_identifiers
    from saebooks.services.companies import ensure_seed_company

    # A random far-future FY keeps reruns against a persistent test DB
    # clean: no earlier run's bills fall inside this aggregation window.
    fy_year = 2200 + secrets.randbelow(1500)
    fy_start, fy_end = date(fy_year, 7, 1), date(fy_year + 1, 6, 30)

    async with AsyncSessionLocal() as session:
        company = await ensure_seed_company(session)
        company = await session.get(Company, company.id)
        company.address = {
            "line1": "1 Example Street", "suburb": "Brisbane",
            "state": "QLD", "postcode": "4000",
        }
        company.phone = company.phone or "0733331234"
        await business_identifiers.upsert(
            session, company.id, "au_abn", VALID_ABN,
            tenant_id=company.tenant_id,
        )
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "BDE Subbie Alex Smith",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="BDE Subbie Alex Smith",
                family_name="Smith",
                given_name="Alex",
                contact_type=ContactType.SUPPLIER,
                abn="51824753556",
                is_tpar_supplier=True,
                address_line1="2 Subbie Street",
                city="Cairns",
                state="QLD",
                postcode="4870",
                phone="0740001234",
                email="alex@example.com",
            )
            session.add(contact)
        await session.commit()
        cid, contact_id, tenant_id = company.id, contact.id, company.tenant_id
        expense = (
            await session.execute(
                select(Account).where(
                    Account.company_id == cid, Account.code == "6-1000"
                )
            )
        ).scalar_one()

    async with AsyncSessionLocal() as session:
        bill = await bill_svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact_id,
            issue_date=date(fy_year, 9, 1),
            due_date=date(fy_year, 9, 1),
            lines=[{
                "description": "subcontract work",
                "account_id": expense.id,
                "tax_code_id": None,
                "quantity": Decimal("1"),
                "unit_price": Decimal("5600.85"),
                "discount_pct": Decimal("0"),
            }],
        )
        await bill_svc.post_bill(session, bill.id, posted_by="tests")

    async with AsyncSessionLocal() as session:
        run_id = await svc.build_tpar_run(
            session, tenant_id=tenant_id, company_id=cid,
            fy_start=fy_start, fy_end=fy_end,
        )

    async with AsyncSessionLocal() as session:
        raw = await svc.build_tpar_bde_file_for_run(
            session, tenant_id=tenant_id, company_id=cid,
            run_id=run_id, run_type="T",
        )

    records = [
        raw.decode("ascii")[i : i + RECORD_LENGTH]
        for i in range(0, len(raw), RECORD_LENGTH)
    ]
    assert len(records) == 7  # 3 headers + IDENTITY + SOFTWARE + payee + total
    assert records[0][28] == "T"
    payee_rec = records[5]
    assert payee_rec[3:9] == "DPAIVS"
    assert payee_rec[20:50].rstrip() == "Smith"
    assert payee_rec[50:65].rstrip() == "Alex"
    assert payee_rec[80:280].strip() == ""  # individual → business name blank
    assert payee_rec[640:651] == "00000005600"  # $5,600.85 truncated (6.61)
    assert records[-1][13:21] == "00000007"
