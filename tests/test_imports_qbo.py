"""Parser tests for QBO contact + account-list importers."""
from __future__ import annotations

import pytest

from saebooks.models.account import AccountType
from saebooks.models.contact import ContactType
from saebooks.services.imports import qbo
from saebooks.services.imports.qbo import QboContactKind, QboImportError


def test_parse_customers_minimal() -> None:
    raw = (
        "Customer,Email,Phone,Billing City,Billing State,Billing Zip\n"
        "Acme Corp,acme@example.com,555-1212,Brisbane,QLD,4000\n"
    )
    rows = qbo.parse_qbo_contacts(raw)
    assert len(rows) == 1
    assert rows[0].name == "Acme Corp"
    assert rows[0].contact_type is ContactType.CUSTOMER
    assert rows[0].email == "acme@example.com"
    assert rows[0].city == "Brisbane"
    assert rows[0].state == "QLD"
    assert rows[0].postcode == "4000"


def test_parse_auto_detects_vendor_header() -> None:
    raw = (
        "Vendor,Email,Phone\n"
        "Supplier X,x@example.com,555-0000\n"
    )
    rows = qbo.parse_qbo_contacts(raw)
    assert rows[0].contact_type is ContactType.SUPPLIER


def test_parse_explicit_kind_override() -> None:
    """Even a CUSTOMER-looking file gets coerced to SUPPLIER when asked."""
    raw = "Customer,Email\nAcme,a@example.com\n"
    rows = qbo.parse_qbo_contacts(raw, kind=QboContactKind.VENDOR)
    assert rows[0].contact_type is ContactType.SUPPLIER


def test_parse_skips_blank_name_rows() -> None:
    raw = (
        "Customer,Email\n"
        ",empty@example.com\n"
        "Acme,a@example.com\n"
    )
    rows = qbo.parse_qbo_contacts(raw)
    assert len(rows) == 1
    assert rows[0].name == "Acme"


def test_parse_falls_back_to_company_column() -> None:
    raw = (
        "Company,Email\n"
        "Acme Corp,a@example.com\n"
    )
    rows = qbo.parse_qbo_contacts(raw)
    assert len(rows) == 1
    assert rows[0].name == "Acme Corp"


def test_parse_accounts_happy_path() -> None:
    raw = (
        "Account,Type,Number\n"
        "Chequing,Bank,1-1100\n"
        "Accounts Receivable,Accounts Receivable,1-1200\n"
        "Sales,Income,4-1000\n"
        "Advertising,Expenses,6-1000\n"
    )
    rows = qbo.parse_qbo_accounts(raw)
    assert len(rows) == 4
    assert rows[0].name == "Chequing"
    assert rows[0].account_type is AccountType.ASSET
    assert rows[0].code == "1-1100"
    assert rows[2].account_type is AccountType.INCOME
    assert rows[3].account_type is AccountType.EXPENSE


def test_parse_accounts_rejects_unknown_type() -> None:
    raw = "Account,Type\nMystery,Fruitcake\n"
    with pytest.raises(QboImportError):
        qbo.parse_qbo_accounts(raw)


def test_parse_accounts_rejects_missing_columns() -> None:
    raw = "Name,Category\nFoo,Bar\n"
    with pytest.raises(QboImportError):
        qbo.parse_qbo_accounts(raw)


def test_qbo_coa_to_rows_synthesises_missing_codes() -> None:
    raw = (
        "Account,Type,Number\n"
        "Chequing,Bank,1-1100\n"
        "Undeposited Funds,Bank,\n"
    )
    qbo_rows = qbo.parse_qbo_accounts(raw)
    coa_rows = qbo.qbo_coa_to_rows(qbo_rows)
    # Second row got a synthesised 9-prefixed code.
    assert coa_rows[0].code == "1-1100"
    assert coa_rows[1].code.startswith("9-")


def test_qbo_coa_to_rows_sets_asset_reconcile_flag() -> None:
    qbo_rows = qbo.parse_qbo_accounts(
        "Account,Type\nChequing,Bank\nSales,Income\n"
    )
    coa_rows = qbo.qbo_coa_to_rows(qbo_rows)
    by_type = {r.account_type: r for r in coa_rows}
    assert by_type[AccountType.ASSET].reconcile is True
    assert by_type[AccountType.INCOME].reconcile is False
