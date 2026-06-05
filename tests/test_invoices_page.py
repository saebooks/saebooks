"""Router smoke tests for ``/invoices``.

Covers:

* list page renders (empty + with data)
* new-invoice form renders (with preview number + contact dropdown)
* POST creates a DRAFT (Server returns redirect to the detail page)
* DRAFT detail page shows Edit / Post / Discard buttons
* Post transition renders POSTED state on detail page
* PDF route returns application/pdf bytes
* Archive route redirects to list
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import respx
from httpx import AsyncClient, Response
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode
from saebooks.services import invoices as svc

pytestmark = pytest.mark.postgres_only


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(
                    Company.created_at
                )
            )
        ).scalars().first()
        assert company is not None

        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company.id,
                    Account.code == "4-6000",
                )
            )
        ).scalar_one()

        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == company.id,
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.name == "Page Test Customer",
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                name="Page Test Customer",
                contact_type=ContactType.CUSTOMER,
                email="page@example.com",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)

        return company.id, contact.id, income.id, gst.id


@pytest.mark.asyncio
async def test_invoices_list_renders(client: AsyncClient) -> None:
    r = await client.get("/invoices")
    assert r.status_code == 200
    assert "Invoices" in r.text


@pytest.mark.asyncio
async def test_invoices_new_form_renders(client: AsyncClient) -> None:
    _cid, contact, _acct, _gst = await _ctx()
    r = await client.get("/invoices/new")
    assert r.status_code == 200
    assert "New invoice" in r.text
    assert "Page Test Customer" in r.text
    assert str(contact) in r.text
    # Preview of the next-number to be allocated on post
    assert "INV-" in r.text


@pytest.mark.asyncio
async def test_invoices_post_creates_draft(client: AsyncClient) -> None:
    _cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    data = {
        "contact_id": str(contact),
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=30)).isoformat(),
        "line_0_description": "Web dev",
        "line_0_account_id": str(acct),
        "line_0_tax_code_id": str(gst),
        "line_0_quantity": "5",
        "line_0_unit_price": "200",
        "line_0_discount_pct": "0",
        "payment_terms": "Net 30",
        "notes": "",
    }
    r = await client.post("/invoices", data=data, follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    # Redirect target is /invoices/<uuid>
    assert r.headers["location"].startswith("/invoices/")


@pytest.mark.asyncio
async def test_invoices_detail_shows_draft_actions(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Consult",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.get(f"/invoices/{inv.id}")
    assert r.status_code == 200
    assert "DRAFT" in r.text
    # DRAFT-specific buttons
    assert "Edit" in r.text
    assert "Post" in r.text
    assert "Discard" in r.text


@pytest.mark.asyncio
async def test_invoice_post_transitions_to_posted(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Line",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.post(f"/invoices/{inv.id}/post", follow_redirects=False)
    assert r.status_code in (302, 303)
    detail = await client.get(f"/invoices/{inv.id}")
    assert detail.status_code == 200
    assert "POSTED" in detail.text
    # POSTED state exposes PDF + email + void, not edit
    assert "PDF" in detail.text
    assert "Email invoice" in detail.text


@pytest.mark.asyncio
async def test_invoice_pdf_renders(
    client: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    """``GET /invoices/{id}.pdf`` renders via the latex-api microservice.

    The route calls ``render_latex("document", ctx)`` which (1) renders the
    real ``document.tex.j2`` Jinja2 template against the invoice context and
    (2) POSTs the LaTeX source to the ``latex-api`` service, then GETs the
    compiled PDF.  latex-api is not reachable from the test stack, so we mock
    it with respx and assert the route streams the compiled bytes back with
    ``application/pdf``.

    ``_svc._env`` is reset to ``None`` so a fresh FileSystemLoader environment
    loads the real on-disk template — this also makes the test immune to a
    DictLoader that ``tests/services/test_latex_pdf.py`` may have left behind
    on the module-global ``_env``.
    """
    import saebooks.services.latex_pdf as _svc

    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "PDF line",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("123.45"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await svc.post_invoice(session, inv.id, posted_by="test")

    # Force a fresh FileSystemLoader env (real document.tex.j2 from disk),
    # immune to any leaked DictLoader from the latex_pdf unit tests.
    _svc._env = None

    fake_pdf = b"%PDF-1.5 fake-invoice-pdf"
    fake_pdf_url = "/files/inv-test.pdf"
    latex_api_base = "http://latex-api:8000"  # settings.latex_api_url default

    respx_mock.post(f"{latex_api_base}/compile").mock(
        return_value=Response(
            200, json={"status": "ok", "pdf_url": fake_pdf_url, "id": "inv1"}
        )
    )
    respx_mock.get(f"{latex_api_base}{fake_pdf_url}").mock(
        return_value=Response(200, content=fake_pdf)
    )

    r = await client.get(f"/invoices/{inv.id}.pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    # latex-api returned the compiled PDF; the route streams it through verbatim.
    assert r.content == fake_pdf
    assert r.content.startswith(b"%PDF")

    # The LaTeX source POSTed to latex-api must contain the rendered invoice
    # data — proves the real document.tex.j2 template was rendered, not bypassed.
    # calls[0] is the POST /compile (calls.last would be the GET pdf fetch).
    compile_call = respx_mock.calls[0]
    assert compile_call.request.method == "POST"
    posted = compile_call.request.content.decode()
    assert "123.45" in posted


@pytest.mark.asyncio
async def test_invoice_archive_redirects(client: AsyncClient) -> None:
    cid, contact, acct, gst = await _ctx()
    today = date(2026, 4, 20)
    async with AsyncSessionLocal() as session:
        inv = await svc.create_draft(
            session,
            company_id=cid,
            contact_id=contact,
            issue_date=today,
            due_date=today + timedelta(days=30),
            lines=[
                {
                    "description": "Archive me",
                    "account_id": acct,
                    "tax_code_id": gst,
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("10"),
                    "discount_pct": Decimal("0"),
                }
            ],
        )
    r = await client.post(f"/invoices/{inv.id}/archive", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/invoices"
