"""Tests for ``saebooks.services.metrics`` + the /metrics endpoint.

Two layers:

* unit tests against ``refresh_gauges`` — prove the COUNT queries
  produce the right numbers for a fresh + dirty DB,
* router smoke tests hitting ``GET /metrics`` — prove the endpoint
  returns 200 text/plain with the expected gauge names.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from saebooks.services import metrics as svc

# ---------------------------------------------------------------------- #
# Unit: refresh_gauges populates the business gauges                      #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_gauges_populates_every_series() -> None:
    await svc.refresh_gauges()
    # After refresh, the metric family has at least one sample
    # per label set. On dev DB there's always one active company.
    open_samples = next(iter(svc.OPEN_INVOICES.collect())).samples
    overdue_samples = next(iter(svc.OVERDUE_INVOICES.collect())).samples
    unmatched_samples = next(iter(svc.UNMATCHED_STATEMENT_LINES.collect())).samples

    assert len(open_samples) >= 1
    assert len(overdue_samples) >= 1
    assert len(unmatched_samples) >= 1

    # Each sample must carry a company_id label and a float value.
    for sample in open_samples:
        assert "company_id" in sample.labels
        assert isinstance(sample.value, float)
        assert sample.value >= 0


# ---------------------------------------------------------------------- #
# Router: GET /metrics                                                    #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    # Prometheus text format 0.0.4 content-type starts with text/plain
    # and carries version + charset suffixes.
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_metrics_exposes_custom_gauges(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    body = r.text
    assert "saebooks_open_invoices_total" in body
    assert "saebooks_overdue_invoices_total" in body
    assert "saebooks_unmatched_statement_lines_total" in body


@pytest.mark.asyncio
async def test_metrics_exposes_http_histograms(client: AsyncClient) -> None:
    # Fire a request first so the histogram has at least one observation.
    # (Any matched route works; /api/v1/healthz is unauthenticated and
    # always available on the pure-API engine.)
    import uuid as _uuid

    await client.get(f"/api/v1/invoices/{_uuid.uuid4()}")
    r = await client.get("/metrics")
    body = r.text
    assert "saebooks_http_request_duration_seconds" in body
    assert "saebooks_http_requests_total" in body


@pytest.mark.asyncio
async def test_metrics_not_counted_in_histogram(client: AsyncClient) -> None:
    """`/metrics` scrapes must not appear in the request histogram.

    Otherwise scraper load dominates the p99 quantile.
    """
    # Baseline: count of http_requests_total samples that mention /metrics.
    before = (await client.get("/metrics")).text
    after = (await client.get("/metrics")).text
    # Neither payload should contain a histogram/counter row for
    # route="/metrics". The middleware early-exits on that path.
    for body in (before, after):
        # the "route" label is how the middleware tags it.
        assert 'route="/metrics"' not in body


@pytest.mark.asyncio
async def test_route_template_keeps_cardinality_bounded(client: AsyncClient) -> None:
    """Two hits to a UUID-carrying route with DIFFERENT ids must collapse
    onto ONE series under the route *template* ``…/invoices/{invoice_id}``
    — not two raw-path series. The template is the only thing stopping
    cardinality blow-up on UUID-heavy routes. (Unauthenticated →
    deterministic 401, recorded against the template just the same.)

    The assertion is prefix-agnostic: the metrics middleware records the
    matched route's ``.path`` template, and the exact rendering of the
    ``/api/v1`` prefix in that label is not what this test pins — the
    cardinality guarantee (placeholder, not raw UUID) is."""
    import uuid as _uuid

    uid_a = _uuid.uuid4()
    uid_b = _uuid.uuid4()
    await client.get(f"/api/v1/invoices/{uid_a}")
    await client.get(f"/api/v1/invoices/{uid_b}")
    r = await client.get("/metrics")
    assert r.status_code == 200
    # The route TEMPLATE placeholder must be recorded …
    assert 'invoices/{invoice_id}"' in r.text, (
        "Expected the {invoice_id} route template in /metrics labels"
    )
    # … and the raw UUIDs must NOT appear as labels (that would mean the
    # middleware tagged the raw path → cardinality blow-up).
    assert str(uid_a) not in r.text
    assert str(uid_b) not in r.text
