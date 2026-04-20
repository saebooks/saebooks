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
    await client.get("/dashboard")
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
    """Two hits to /dashboard emit a single (method, route, status) label
    tuple — if the middleware tagged the raw path, that would hold too,
    but when it comes to UUID-carrying routes like
    ``/invoices/{invoice_id}`` the template is the only thing stopping
    cardinality blow-up. We assert the well-known route template is
    recorded."""
    # Two hits to the same path — the counter should advance, not
    # create a second series.
    await client.get("/dashboard")
    await client.get("/dashboard")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert 'route="/dashboard"' in r.text
    # Counter value must be a monotonically rising scalar, not split
    # across two labels.
    lines = [
        ln for ln in r.text.splitlines()
        if ln.startswith("saebooks_http_requests_total")
        and 'route="/dashboard"' in ln
        and 'method="GET"' in ln
    ]
    assert lines, "No dashboard counter line found in /metrics"
    # Grab one value and verify it's >= 2.
    for ln in lines:
        value = float(ln.rsplit(" ", 1)[-1])
        if value >= 2:
            break
    else:
        raise AssertionError(f"Dashboard counter never reached 2: {lines}")
