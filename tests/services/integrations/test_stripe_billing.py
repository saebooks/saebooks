"""Unit tests for saebooks.services.integrations.stripe_billing.

We don't hit the live Stripe API — the module's ``_client()`` builds a
real ``httpx.AsyncClient`` against ``api.stripe.com``, so we
monkeypatch ``_client`` to return a mock client that records calls and
returns canned responses. That covers the two things we actually want
to verify:

1. ``EDITIONS`` shape — keyed by edition, nested ``prices`` dict by
   period, top-level ``name``/``description``/``currency``.
2. ``create_checkout_session(period=...)`` searches Stripe with both
   ``sae_edition`` AND ``sae_period`` metadata filters and uses the
   matching price.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from saebooks.services.integrations import stripe_billing
from saebooks.services.integrations.stripe_billing import (
    EDITIONS,
    StripeBillingError,
    create_checkout_session,
)


# ---------------------------------------------------------------------------
# EDITIONS shape
# ---------------------------------------------------------------------------


def test_editions_shape_has_per_period_prices() -> None:
    """Every edition has top-level name/description/currency and a
    nested ``prices`` dict keyed by period → cents."""
    for edition in ("business", "pro"):
        spec = EDITIONS[edition]
        assert isinstance(spec["name"], str)
        assert isinstance(spec["description"], str)
        assert spec["currency"] == "aud"
        prices = spec["prices"]
        assert set(prices.keys()) == {"month", "year"}
        for amount in prices.values():
            assert isinstance(amount, int)
            assert amount > 0


def test_editions_yearly_is_ten_times_monthly() -> None:
    """Honest 'save 2 months' framing: yearly = monthly * 10."""
    for edition in ("business", "pro"):
        spec = EDITIONS[edition]
        assert spec["prices"]["year"] == spec["prices"]["month"] * 10


def test_editions_business_amounts_match_marketing_site() -> None:
    assert EDITIONS["business"]["prices"]["month"] == 4900
    assert EDITIONS["business"]["prices"]["year"] == 49000


def test_editions_pro_amounts_match_marketing_site() -> None:
    assert EDITIONS["pro"]["prices"]["month"] == 9900
    assert EDITIONS["pro"]["prices"]["year"] == 99000


# ---------------------------------------------------------------------------
# create_checkout_session period plumbing
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for httpx.Response — enough surface for the helpers."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeStripeClient:
    """Records GET/POST calls; returns canned data based on the path
    and search query. Async-context-manager compatible."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, list[tuple[str, str]]]] = []
        # Default: searches return one matching price; checkout/sessions
        # POST returns a cs_test_… session.
        self._price_id = "price_TEST_yearly_business_id"
        self._session_id = "cs_test_yearly_session_id"
        self._session_url = "https://checkout.stripe.com/c/pay/cs_test_xyz"

    async def __aenter__(self) -> "_FakeStripeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.get_calls.append((path, dict(params or {})))
        if path.endswith("/search"):
            # Return one price. Tests inspect get_calls[0][1]['query'] for
            # the metadata filter.
            return _FakeResponse(
                {
                    "data": [
                        {
                            "id": self._price_id,
                            "unit_amount": 49000,
                            "currency": "aud",
                            "recurring": {"interval": "year"},
                            "metadata": {
                                "sae_edition": "business",
                                "sae_period": "year",
                            },
                            "product": "prod_TEST_business",
                            "active": True,
                        }
                    ]
                }
            )
        return _FakeResponse({"data": []})

    async def post(
        self,
        path: str,
        *,
        content: bytes | None = None,
        data: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        # _post in stripe_billing now sends bodies as urlencoded bytes
        # via content= (httpx 0.28 + tuple-auth + data= bug
        # workaround). Decode them back into key/value pairs so tests
        # can assert on form fields the same way.
        if content is not None:
            from urllib.parse import parse_qsl
            pairs = parse_qsl(content.decode("utf-8"), keep_blank_values=True)
        else:
            pairs = list(data or [])
        self.post_calls.append((path, pairs))
        if path == "/checkout/sessions":
            return _FakeResponse(
                {"id": self._session_id, "url": self._session_url}
            )
        return _FakeResponse({"id": "ignored"})


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> _FakeStripeClient:
    fake = _FakeStripeClient()

    def _factory() -> _FakeStripeClient:
        return fake

    monkeypatch.setattr(stripe_billing, "_client", _factory)
    return fake


async def test_create_checkout_session_year_searches_with_period_metadata(
    fake_stripe: _FakeStripeClient,
) -> None:
    """The yearly checkout path searches Stripe with both
    ``sae_edition`` AND ``sae_period:'year'`` filters, and the
    resulting checkout session line-item references the price ID
    that came back from that search."""
    result = await create_checkout_session(
        edition="business",
        customer_email="buyer@example.test",
        period="year",
    )

    # 1. Search query carried both metadata filters.
    assert len(fake_stripe.get_calls) == 1
    path, params = fake_stripe.get_calls[0]
    assert path == "/prices/search"
    query = params["query"]
    assert "metadata['sae_edition']:'business'" in query
    assert "metadata['sae_period']:'year'" in query
    assert "active:'true'" in query

    # 2. Checkout session POST referenced the searched price ID and
    #    stamped both metadata fields on the session + subscription_data.
    assert len(fake_stripe.post_calls) == 1
    post_path, form_pairs = fake_stripe.post_calls[0]
    assert post_path == "/checkout/sessions"
    form = dict(form_pairs)
    assert form["line_items[0][price]"] == "price_TEST_yearly_business_id"
    assert form["mode"] == "subscription"
    assert form["customer_email"] == "buyer@example.test"
    assert form["metadata[sae_edition]"] == "business"
    assert form["metadata[sae_period]"] == "year"
    assert form["subscription_data[metadata][sae_edition]"] == "business"
    assert form["subscription_data[metadata][sae_period]"] == "year"

    # 3. Returned URL + session ID surfaced from Stripe response.
    assert result["session_id"] == "cs_test_yearly_session_id"
    assert result["checkout_url"].startswith("https://checkout.stripe.com/")


async def test_create_checkout_session_month_default_period(
    fake_stripe: _FakeStripeClient,
) -> None:
    """Default period is 'month' — backwards compat with old callers."""
    await create_checkout_session(
        edition="pro",
        customer_email="buyer@example.test",
    )
    _, params = fake_stripe.get_calls[0]
    assert "metadata['sae_period']:'month'" in params["query"]
    assert "metadata['sae_edition']:'pro'" in params["query"]


async def test_create_checkout_session_rejects_unknown_period() -> None:
    with pytest.raises(StripeBillingError, match="period"):
        await create_checkout_session(
            edition="business",
            customer_email="x@y.test",
            period="weekly",  # type: ignore[arg-type]
        )


async def test_create_checkout_session_rejects_unknown_edition() -> None:
    with pytest.raises(StripeBillingError, match="edition"):
        await create_checkout_session(
            edition="enterprise",  # type: ignore[arg-type]
            customer_email="x@y.test",
            period="month",
        )
