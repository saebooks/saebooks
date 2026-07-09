"""Contract-drift regression tests for saebooks/mcp/server.py.

Background: a drift audit found MCP tool bodies that no longer match the
v1 REST API's Pydantic schemas (wrong field names, wrong nesting, wrong
enum casing) — see the 2026-07-03 drift fix pass. These tests pin the
fix by calling each CREATE-style MCP tool with its HTTP helpers
(``_post``/``_patch``/``_get``) monkeypatched to capture the exact body
that would be sent over the wire, then validating that body directly
against the corresponding ``saebooks.api.v1.schemas`` Pydantic model —
the same validation the real API would perform. If a tool drifts from
the schema again, ``model_validate`` raises and the test fails.

Follows the introspect-the-live-registry convention from
``test_mcp_mount.py`` / ``test_mcp_no_manual_je.py``: the tool
functions are imported directly (FastMCP's ``mcp.tool()`` decorator
returns the original function unchanged, so ``mcp_server.create_expense``
etc. are plain async callables) rather than going through the MCP
protocol layer, which would need a live server.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from saebooks.api.v1 import schemas
from saebooks.mcp import server as mcp_server


def _uuid() -> str:
    return str(uuid.uuid4())


class _Capture:
    """Records the last body/params sent through the patched HTTP helpers."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.body: dict[str, Any] | None = None
        self.params: dict[str, Any] | None = None


@pytest.fixture()
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """Patch _post/_patch/_get on the mcp_server module so calling a tool
    never makes a real HTTP request — it just records what it WOULD have
    sent, which is exactly the thing under test.
    """
    cap = _Capture()

    async def fake_post(
        ctx: Any,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        if_match: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        cap.path = path
        cap.body = body or {}
        return {"id": _uuid(), "version": 1}

    async def fake_patch(
        ctx: Any, path: str, body: dict[str, Any], *, if_match: Any = None
    ) -> dict[str, Any]:
        cap.path = path
        cap.body = body
        return {"id": _uuid(), "version": 2}

    async def fake_get(ctx: Any, path: str, **params: Any) -> dict[str, Any]:
        cap.path = path
        cap.params = params
        return {"items": [], "total": 0}

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_patch", fake_patch)
    monkeypatch.setattr(mcp_server, "_get", fake_get)
    return cap


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


async def test_create_expense_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_expense(
        ctx=None,
        expense_date="2026-07-01",
        payment_account_id=_uuid(),
        lines=[
            {
                "description": "Fuel",
                "account_id": _uuid(),
                "quantity": 1,
                "unit_price": 55.5,
            }
        ],
        contact_id=_uuid(),
        reference="RCPT-001",
        notes="fuel top-up",
    )
    assert capture.path == "/api/v1/expenses"
    schemas.ExpenseCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------


async def test_create_bill_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_bill(
        ctx=None,
        contact_id=_uuid(),
        issue_date="2026-07-01",
        due_date="2026-07-31",
        reference="INV-9001",
        lines=[
            {
                "description": "Widgets",
                "account_id": _uuid(),
                "quantity": 10,
                "unit_price": 12.0,
            }
        ],
        currency="AUD",
        notes="supplier bill",
    )
    assert capture.path == "/api/v1/bills"
    # The MCP-facing 'reference' arg must land on supplier_reference, not
    # a bogus 'reference' key.
    assert "reference" not in capture.body
    assert capture.body.get("supplier_reference") == "INV-9001"
    schemas.BillCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


async def test_create_invoice_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_invoice(
        ctx=None,
        contact_id=_uuid(),
        issue_date="2026-07-01",
        due_date="2026-07-31",
        lines=[
            {
                "description": "Consulting",
                "account_id": _uuid(),
                "quantity": 1,
                "unit_price": 500,
            }
        ],
        currency="AUD",
        notes="",
    )
    assert capture.path == "/api/v1/invoices"
    assert "reference" not in capture.body
    schemas.InvoiceCreate.model_validate(capture.body)


def test_create_invoice_requires_due_date() -> None:
    """due_date has no server-side default — the tool signature must not
    let a caller silently omit it."""
    import inspect

    sig = inspect.signature(mcp_server.create_invoice)
    assert sig.parameters["due_date"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------


async def test_create_credit_note_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_credit_note(
        ctx=None,
        contact_id=_uuid(),
        issue_date="2026-07-01",
        lines=[
            {
                "description": "Refund",
                "account_id": _uuid(),
                "quantity": 1,
                "unit_price": 50,
            }
        ],
        reason="customer refund",
        notes="",
        original_invoice_id=_uuid(),
    )
    assert capture.path == "/api/v1/credit_notes"
    assert "invoice_id" not in capture.body
    assert "reference" not in capture.body
    schemas.CreditNoteCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


async def test_create_item_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_item(
        ctx=None,
        sku="WIDGET-001",
        name="Widget",
        inventory_account_id=_uuid(),
        cogs_account_id=_uuid(),
        income_account_id=_uuid(),
        item_type="inventory",
        description="A widget",
        cost_method="WAC",
        default_sale_price=19.99,
        on_hand_qty=100,
        wac_cost=5.0,
    )
    assert capture.path == "/api/v1/items"
    assert "code" not in capture.body
    assert "tracked" not in capture.body
    schemas.ItemCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


async def test_create_quote_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_quote(
        ctx=None,
        customer_id=_uuid(),
        issue_date="2026-07-01",
        lines=[{"description": "Fence install", "quantity": 1, "unit_price": 2500}],
        expiry_date="2026-08-01",
        notes="",
    )
    assert capture.path == "/api/v1/quotes"
    assert "contact_id" not in capture.body
    assert "reference" not in capture.body
    assert capture.body.get("customer_id")
    schemas.QuoteCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


async def test_create_payment_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_payment(
        ctx=None,
        payment_date="2026-07-01",
        bank_account_id=_uuid(),
        amount=100.0,
        direction="INCOMING",
        contact_id=_uuid(),
        allocations=[],
        reference="EFT-1",
        notes="",
    )
    assert capture.path == "/api/v1/payments"
    assert "contact_id" in capture.body  # REQUIRED, always sent
    schemas.PaymentCreate.model_validate(capture.body)


def test_create_payment_requires_contact_id() -> None:
    import inspect

    sig = inspect.signature(mcp_server.create_payment)
    assert sig.parameters["contact_id"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Bank statement lines
# ---------------------------------------------------------------------------


async def test_create_bank_statement_line_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_bank_statement_line(
        ctx=None,
        account_id=_uuid(),
        txn_date="2026-07-01",
        amount=-42.5,
        description="EFTPOS purchase",
        reference="",
    )
    assert capture.path == "/api/v1/bank_statement_lines"
    assert "bank_account_id" not in capture.body
    assert "transaction_date" not in capture.body
    schemas.BankStatementLineCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Bank rules
# ---------------------------------------------------------------------------


async def test_create_bank_rule_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_bank_rule(
        ctx=None,
        name="Fuel auto-categorise",
        match_type="CONTAINS",
        match_pattern="BP FUEL",
        account_id=_uuid(),
        tax_code="GST",
        contact_id="",
    )
    assert capture.path == "/api/v1/bank_rules"
    assert "match_value" not in capture.body
    assert "target_account_id" not in capture.body
    assert "bank_account_id" not in capture.body
    schemas.BankRuleCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Allocation rules
# ---------------------------------------------------------------------------


async def test_create_allocation_rule_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_allocation_rule(
        ctx=None,
        name="Home office split",
        source_account_id=_uuid(),
        targets=[
            {"account_id": _uuid(), "label": "Business use", "percentage": 70},
            {"account_id": _uuid(), "label": "Personal use", "percentage": 30},
        ],
        description="70/30 split",
    )
    assert capture.path == "/api/v1/allocation_rules"
    assert "splits" not in capture.body
    schemas.AllocationRuleCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def test_create_account_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_account(
        ctx=None,
        code="6-1234",
        name="Test Expense Account",
        account_type="EXPENSE",
        parent_id="",
        tax_code_default="GST",
        is_header=False,
        reconcile=False,
    )
    assert capture.path == "/api/v1/accounts"
    assert "description" not in capture.body
    assert "tax_code_id" not in capture.body
    schemas.AccountCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Tax codes
# ---------------------------------------------------------------------------


async def test_create_tax_code_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_tax_code(
        ctx=None,
        code="GST",
        name="GST 10%",
        rate=10.0,
        tax_system="GST",
        reporting_type="taxable",
        description="Standard GST",
    )
    assert capture.path == "/api/v1/tax_codes"
    assert "tax_account_id" not in capture.body
    schemas.TaxCodeCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


async def test_create_contact_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_contact(
        ctx=None,
        name="Acme Pty Ltd",
        contact_type="CUSTOMER",
        email="ap@acme.example",
        phone="0400000000",
        abn="12345678901",
        address={
            "line1": "1 Example St",
            "line2": "",
            "city": "Cairns",
            "state": "QLD",
            "postcode": "4870",
            "country": "Australia",
        },
    )
    assert capture.path == "/api/v1/contacts"
    assert "address" not in capture.body
    assert capture.body.get("address_line1") == "1 Example St"
    assert capture.body.get("city") == "Cairns"
    schemas.ContactCreate.model_validate(capture.body)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


async def test_create_project_body_matches_schema(capture: _Capture) -> None:
    await mcp_server.create_project(
        ctx=None,
        code="JOB-001",
        name="Front fence job",
        status="ACTIVE",
        start_date="2026-07-01",
        end_date="",
        notes="",
    )
    assert capture.path == "/api/v1/projects"
    assert "contact_id" not in capture.body
    assert "default_billable" not in capture.body
    assert "default_rate" not in capture.body
    schemas.ProjectCreate.model_validate(capture.body)


def test_create_project_requires_code_and_name() -> None:
    import inspect

    sig = inspect.signature(mcp_server.create_project)
    assert sig.parameters["code"].default is inspect.Parameter.empty
    assert sig.parameters["name"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Imports (multi-step wizard tools)
#
# The old start_import/list_imports POSTed/GET /api/v1/imports, which does
# not exist — the real surface is the multi-step /api/v1/imports/wizards*.
# These pin the fix: the tools must drive the real wizard routes and their
# bodies must validate against the wizard request schemas.
# ---------------------------------------------------------------------------


class _MultiCapture:
    """Records every _post/_get call (start_import fires two _posts)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []


@pytest.fixture()
def multi_capture(monkeypatch: pytest.MonkeyPatch) -> _MultiCapture:
    cap = _MultiCapture()

    async def fake_post(
        ctx: Any,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        if_match: Any = None,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        cap.posts.append((path, body or {}))
        # First POST is the wizard create → return a wizard_id + step/state
        # so start_import can drive the follow-up step call.
        if path.endswith("/wizards"):
            return {"wizard_id": _uuid(), "step": 0, "state": {"step": 0}}
        return {"step": 1, "state": {"step": 1}, "completed": False}

    async def fake_get(ctx: Any, path: str, **params: Any) -> dict[str, Any]:
        cap.gets.append((path, params))
        return {"wizards": [], "total": 0}

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    monkeypatch.setattr(mcp_server, "_get", fake_get)
    return cap


async def test_start_import_drives_wizard_flow(multi_capture: _MultiCapture) -> None:
    """start_import must create a wizard then upload the payload as a step,
    with bodies matching WizardStartBody / WizardStepBody."""
    from saebooks.api.v1.imports import WizardStartBody, WizardStepBody

    result = await mcp_server.start_import(
        ctx=None,
        kind="bank_csv",
        raw="date,amount,description\n2026-07-01,-42.50,Coffee\n",
        account_id=_uuid(),
    )

    # Two POSTs: create wizard, then upload step.
    assert len(multi_capture.posts) == 2
    create_path, create_body = multi_capture.posts[0]
    step_path, step_body = multi_capture.posts[1]

    assert create_path == "/api/v1/imports/wizards"
    WizardStartBody.model_validate(create_body)
    assert create_body["kind"] == "bank_csv"
    # account_id must be threaded into initial state, not a bogus top-level key.
    assert "account_id" in create_body["initial"]

    assert step_path.startswith("/api/v1/imports/wizards/")
    assert step_path.endswith("/step")
    WizardStepBody.model_validate(step_body)
    assert step_body["patch"].get("raw")  # the CSV body landed in the patch

    assert result["wizard_id"]
    assert result["next"].endswith("/commit")


async def test_start_import_qbo_uploads_both_blobs(multi_capture: _MultiCapture) -> None:
    """QBO wizard reads contacts_raw / accounts_raw, not a single raw blob."""
    from saebooks.api.v1.imports import WizardStepBody

    await mcp_server.start_import(
        ctx=None,
        kind="qbo",
        contacts_raw="name,email\nAcme,ap@acme.example\n",
        accounts_raw="code,name\n1-1000,Cash\n",
    )
    assert len(multi_capture.posts) == 2
    _, step_body = multi_capture.posts[1]
    WizardStepBody.model_validate(step_body)
    assert step_body["patch"].get("contacts_raw")
    assert step_body["patch"].get("accounts_raw")
    assert "raw" not in step_body["patch"]


async def test_start_import_rejects_unknown_kind(multi_capture: _MultiCapture) -> None:
    with pytest.raises(ValueError, match="Unknown import kind"):
        await mcp_server.start_import(ctx=None, kind="totally_bogus")
    # No HTTP calls made on a bad kind.
    assert multi_capture.posts == []


async def test_start_import_without_payload_defers_upload(
    multi_capture: _MultiCapture,
) -> None:
    """With no raw payload, start_import only creates the wizard and tells
    the caller to upload+commit next."""
    result = await mcp_server.start_import(ctx=None, kind="coa")
    assert len(multi_capture.posts) == 1
    assert multi_capture.posts[0][0] == "/api/v1/imports/wizards"
    assert "/step" in result["next"]


async def test_list_imports_hits_wizard_list_route(
    multi_capture: _MultiCapture,
) -> None:
    """list_imports must GET the real wizard-list route, not /api/v1/imports."""
    await mcp_server.list_imports(ctx=None, kind="bank_csv", limit=25)
    assert len(multi_capture.gets) == 1
    path, params = multi_capture.gets[0]
    assert path == "/api/v1/imports/wizards"
    assert params.get("kind") == "bank_csv"
    assert params.get("limit") == 25


def test_import_tools_no_longer_reference_dead_endpoint() -> None:
    """Guard against regressing to the non-existent /api/v1/imports surface."""
    import inspect

    for fn in (mcp_server.start_import, mcp_server.list_imports):
        src = inspect.getsource(fn)
        # The dead endpoint was exactly "/api/v1/imports" (no /wizards).
        assert '"/api/v1/imports"' not in src
        assert "/api/v1/imports/wizards" in src
