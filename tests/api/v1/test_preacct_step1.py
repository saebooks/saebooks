"""Step-1 pre-accounting fact/hand-off API tests (#32).

Additive engine surface, no extraction yet:

* ``source_quote_id`` on ``POST /api/v1/invoices`` — stamped when supplied,
  NULL when omitted, and the quote→invoice conversion still stamps it.
* ``POST /api/v1/invoices/{id}/lines`` — DRAFT-only single-line append that
  returns the created line's id, bumps version, writes a change_log row, and
  422s on a non-DRAFT invoice.
* ``POST /internal/numbering/next`` — token gate (503 unconfigured / 401 bad
  token), increments per (company, kind), and rejects gap-free statutory
  kinds (invoice / credit_note / receipt) with 422.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def deps() -> dict[str, str]:
    """Active-company-scoped income account + customer (mirrors quote_deps).

    Scopes to the company ``get_active_company_id`` falls back to (oldest by
    ``created_at``) so invoice + quote creates share one company and the
    cross-company FK guards don't trip.
    """
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                ).order_by(Company.created_at).limit(1)
            )
        ).scalars().first()
        assert company is not None, "Seed company missing"

        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                    Account.company_id == company.id,
                ).limit(1)
            )
        ).scalars().first()

        customer = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.company_id == company.id,
                    Contact.contact_type == ContactType.CUSTOMER,
                ).limit(1)
            )
        ).scalars().first()
        if customer is None:
            customer = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Preacct Step1 Customer",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(customer)
            await session.commit()
            await session.refresh(customer)

    assert income is not None, "Test DB has no INCOME account for the active company"
    return {
        "company_id": str(company.id),
        "income_account_id": str(income.id),
        "customer_id": str(customer.id),
    }


def _invoice_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["customer_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Preacct step1 invoice",
        "lines": [
            {
                "description": "Consulting services",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


def _quote_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "customer_id": deps["customer_id"],
        "issue_date": "2026-05-01",
        "expiry_date": "2026-05-29",
        "notes": "Preacct step1 quote",
        "lines": [
            {
                "description": "Engineering services — design",
                "quantity": "10",
                "unit_price": "150.00",
                "account_id": deps["income_account_id"],
            },
        ],
    }
    base.update(overrides)
    return base


async def _make_quote_id(api_client: AsyncClient, deps: dict[str, str]) -> str:
    r = await api_client.post("/api/v1/quotes", json=_quote_payload(deps))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# source_quote_id on invoice create
# ---------------------------------------------------------------------------


async def test_create_stamps_source_quote_id(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    quote_id = await _make_quote_id(api_client, deps)
    r = await api_client.post(
        "/api/v1/invoices",
        json=_invoice_payload(deps, source_quote_id=quote_id),
    )
    assert r.status_code == 201, r.text
    assert r.json()["source_quote_id"] == quote_id

    # And it round-trips on GET.
    invoice_id = r.json()["id"]
    g = await api_client.get(f"/api/v1/invoices/{invoice_id}")
    assert g.status_code == 200
    assert g.json()["source_quote_id"] == quote_id


async def test_create_source_quote_id_null_when_omitted(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    assert r.status_code == 201, r.text
    assert r.json()["source_quote_id"] is None


async def test_create_rejects_foreign_source_quote_id(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """A source_quote_id that names no in-tenant quote is 422, not a 500."""
    r = await api_client.post(
        "/api/v1/invoices",
        json=_invoice_payload(deps, source_quote_id=str(uuid.uuid4())),
    )
    assert r.status_code == 422, r.text


async def test_quote_conversion_still_stamps_source_quote_id(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """The convert-to-invoice hand-off keeps stamping source_quote_id
    after being refactored to fold the stamp into api_create."""
    quote_id = await _make_quote_id(api_client, deps)
    v = (await api_client.get(f"/api/v1/quotes/{quote_id}")).json()["version"]

    r = await api_client.post(
        f"/api/v1/quotes/{quote_id}/send", headers={"If-Match": str(v)}
    )
    v = r.json()["version"]
    r = await api_client.post(
        f"/api/v1/quotes/{quote_id}/accept", headers={"If-Match": str(v)}
    )
    v = r.json()["version"]
    r = await api_client.post(
        f"/api/v1/quotes/{quote_id}/convert-to-invoice", headers={"If-Match": str(v)}
    )
    assert r.status_code == 200, r.text
    invoice_id = r.json()["invoice_id"]

    inv = (await api_client.get(f"/api/v1/invoices/{invoice_id}")).json()
    assert inv["source_quote_id"] == quote_id


# ---------------------------------------------------------------------------
# POST /api/v1/invoices/{id}/lines — append
# ---------------------------------------------------------------------------


async def test_line_append_happy_path(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    assert r.status_code == 201
    inv = r.json()
    invoice_id = inv["id"]
    v = inv["version"]
    assert len(inv["lines"]) == 1

    ap = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/lines",
        headers={"If-Match": str(v)},
        json={
            "description": "Extra work",
            "account_id": deps["income_account_id"],
            "quantity": "2",
            "unit_price": "100.00",
        },
    )
    assert ap.status_code == 201, ap.text
    body = ap.json()
    assert body["invoice_id"] == invoice_id
    assert body["invoice_version"] == v + 1
    # The created line is returned with a real id + line_no == 2.
    assert uuid.UUID(body["line"]["id"])
    assert body["line"]["line_no"] == 2
    assert body["line"]["description"] == "Extra work"

    # The invoice now has 2 lines and recalculated totals (500 + 200 = 700).
    g = (await api_client.get(f"/api/v1/invoices/{invoice_id}")).json()
    assert len(g["lines"]) == 2
    assert g["subtotal"] == "700.00"
    assert g["version"] == v + 1


async def test_line_append_requires_if_match(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    invoice_id = r.json()["id"]
    ap = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/lines",
        json={"description": "x", "account_id": deps["income_account_id"]},
    )
    assert ap.status_code == 428


async def test_line_append_stale_if_match_409(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    invoice_id = r.json()["id"]
    ap = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/lines",
        headers={"If-Match": "999"},
        json={"description": "x", "account_id": deps["income_account_id"]},
    )
    assert ap.status_code == 409


async def test_line_append_non_draft_422(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    """Appending to a POSTED invoice is rejected 422."""
    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    inv = r.json()
    invoice_id = inv["id"]
    v = inv["version"]

    posted = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/post", headers={"If-Match": str(v)}
    )
    assert posted.status_code == 200, posted.text
    pv = posted.json()["version"]

    ap = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/lines",
        headers={"If-Match": str(pv)},
        json={"description": "late", "account_id": deps["income_account_id"]},
    )
    assert ap.status_code == 422, ap.text


async def test_line_append_writes_change_log(
    api_client: AsyncClient, deps: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/invoices", json=_invoice_payload(deps))
    invoice_id = r.json()["id"]
    v = r.json()["version"]
    ap = await api_client.post(
        f"/api/v1/invoices/{invoice_id}/lines",
        headers={"If-Match": str(v)},
        json={"description": "y", "account_id": deps["income_account_id"]},
    )
    assert ap.status_code == 201

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ChangeLog)
                    .where(
                        ChangeLog.id > before,
                        ChangeLog.entity_id == uuid.UUID(invoice_id),
                        ChangeLog.entity == "invoice",
                    )
                    .order_by(ChangeLog.id)
                )
            ).scalars().all()
        )
    assert [row.op for row in rows] == ["create", "update"]
    assert rows[0].version == 1
    assert rows[1].version == 2


# ---------------------------------------------------------------------------
# POST /internal/numbering/next
# ---------------------------------------------------------------------------


@pytest.fixture
async def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    tok = "test-internal-token-preacct1"
    monkeypatch.setattr(settings, "internal_api_token", tok)
    return tok


@pytest.fixture
async def numbering_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_numbering_503_when_unconfigured(
    numbering_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, seeded_company
) -> None:
    monkeypatch.setattr(settings, "internal_api_token", "")
    company_id, _tenant, _accts = seeded_company
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": "anything"},
        json={"company_id": str(company_id), "kind": "quote"},
    )
    assert r.status_code == 503


async def test_numbering_401_on_bad_token(
    numbering_client: AsyncClient, internal_token: str, seeded_company
) -> None:
    company_id, _tenant, _accts = seeded_company
    # Missing header.
    r = await numbering_client.post(
        "/internal/numbering/next",
        json={"company_id": str(company_id), "kind": "quote"},
    )
    assert r.status_code == 401
    # Wrong header.
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": "wrong"},
        json={"company_id": str(company_id), "kind": "quote"},
    )
    assert r.status_code == 401


async def test_numbering_increments(
    numbering_client: AsyncClient, internal_token: str, seeded_company
) -> None:
    company_id, _tenant, _accts = seeded_company
    headers = {"X-Internal-Token": internal_token}
    r1 = await numbering_client.post(
        "/internal/numbering/next",
        headers=headers,
        json={"company_id": str(company_id), "kind": "quote"},
    )
    assert r1.status_code == 200, r1.text
    n1 = r1.json()["number"]
    r2 = await numbering_client.post(
        "/internal/numbering/next",
        headers=headers,
        json={"company_id": str(company_id), "kind": "quote"},
    )
    assert r2.status_code == 200, r2.text
    n2 = r2.json()["number"]

    # Fresh company → gap-free start; consecutive calls advance by exactly 1.
    assert n1 == "Q-000001"
    assert n2 == "Q-000002"


async def test_numbering_purchase_order_kind_ok(
    numbering_client: AsyncClient, internal_token: str, seeded_company
) -> None:
    company_id, _tenant, _accts = seeded_company
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": internal_token},
        json={"company_id": str(company_id), "kind": "purchase_order"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["number"] == "PO-000001"


@pytest.mark.parametrize("kind", ["invoice", "credit_note", "receipt"])
async def test_numbering_rejects_gap_free_kinds(
    numbering_client: AsyncClient, internal_token: str, seeded_company, kind: str
) -> None:
    company_id, _tenant, _accts = seeded_company
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": internal_token},
        json={"company_id": str(company_id), "kind": kind},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "gap_free_kind"


async def test_numbering_unknown_kind_422(
    numbering_client: AsyncClient, internal_token: str, seeded_company
) -> None:
    company_id, _tenant, _accts = seeded_company
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": internal_token},
        json={"company_id": str(company_id), "kind": "not_a_real_kind"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "unknown_kind"


async def test_numbering_unknown_company_404(
    numbering_client: AsyncClient, internal_token: str
) -> None:
    r = await numbering_client.post(
        "/internal/numbering/next",
        headers={"X-Internal-Token": internal_token},
        json={"company_id": str(uuid.uuid4()), "kind": "quote"},
    )
    assert r.status_code == 404, r.text
