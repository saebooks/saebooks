"""Prometheus metrics — gauges + per-request latency histogram.

Exposes three business-level gauges kept fresh on every scrape and
one request-level histogram wrapped around every HTTP request.

Endpoint shape:

    GET /metrics  →  Prometheus text format 0.0.4, 200 OK

The gauges refresh inline on every ``/metrics`` scrape so we don't
run a ticker: Prometheus polls every 15-60 seconds anyway, and the
"refresh on scrape" pattern means the values in Grafana are always
fresh, not stale-by-N-seconds. This costs three ``COUNT(*)`` queries
per scrape — negligible compared to a real user request.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from fastapi import FastAPI
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from saebooks.db import AsyncSessionLocal
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.models.invoice import Invoice, InvoiceStatus

_LOG = logging.getLogger("saebooks.metrics")

# Single shared registry so our gauges + histogram + the default
# process collectors all land on the same /metrics page.
_REGISTRY: CollectorRegistry = REGISTRY


# ---------------------------------------------------------------------- #
# Business gauges — labelled by company_id so multi-company Grafana       #
# dashboards can slice per-tenant.                                        #
# ---------------------------------------------------------------------- #


OPEN_INVOICES = Gauge(
    "saebooks_open_invoices_total",
    "Count of POSTED invoices with balance_due > 0",
    ["company_id"],
    registry=_REGISTRY,
)
OVERDUE_INVOICES = Gauge(
    "saebooks_overdue_invoices_total",
    "Count of POSTED invoices whose due_date has passed and balance_due > 0",
    ["company_id"],
    registry=_REGISTRY,
)
UNMATCHED_STATEMENT_LINES = Gauge(
    "saebooks_unmatched_statement_lines_total",
    "Count of bank statement lines still awaiting a match",
    ["company_id"],
    registry=_REGISTRY,
)


# ---------------------------------------------------------------------- #
# HTTP request histogram + counter                                        #
# ---------------------------------------------------------------------- #


# Labels kept low-cardinality on purpose: raw paths like
# /invoices/<uuid> would explode the series, so we record the route
# *template* instead (e.g. /invoices/{invoice_id}).
REQUEST_LATENCY = Histogram(
    "saebooks_http_request_duration_seconds",
    "HTTP request latency by method + route + status",
    ["method", "route", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=_REGISTRY,
)
REQUEST_COUNT = Counter(
    "saebooks_http_requests_total",
    "Total HTTP requests by method + route + status",
    ["method", "route", "status"],
    registry=_REGISTRY,
)


# ---------------------------------------------------------------------- #
# Gauge refresh                                                           #
# ---------------------------------------------------------------------- #


async def refresh_gauges() -> None:
    """Recompute all business gauges from Postgres.

    Runs inline on every /metrics scrape. Three index-backed
    ``COUNT(*)`` queries per company — cheap.
    """
    async with AsyncSessionLocal() as session:
        companies = (
            await session.execute(
                select(Company.id).where(Company.archived_at.is_(None))
            )
        ).scalars().all()

        today = date.today()
        for cid in companies:
            label = str(cid)

            open_count = (
                await session.execute(
                    select(func.count(Invoice.id)).where(
                        Invoice.company_id == cid,
                        Invoice.status == InvoiceStatus.POSTED,
                        Invoice.archived_at.is_(None),
                        Invoice.total > Invoice.amount_paid,
                    )
                )
            ).scalar_one()
            OPEN_INVOICES.labels(company_id=label).set(int(open_count))

            overdue_count = (
                await session.execute(
                    select(func.count(Invoice.id)).where(
                        Invoice.company_id == cid,
                        Invoice.status == InvoiceStatus.POSTED,
                        Invoice.archived_at.is_(None),
                        Invoice.total > Invoice.amount_paid,
                        Invoice.due_date < today,
                    )
                )
            ).scalar_one()
            OVERDUE_INVOICES.labels(company_id=label).set(int(overdue_count))

            unmatched_count = (
                await session.execute(
                    select(func.count(BankStatementLine.id)).where(
                        BankStatementLine.company_id == cid,
                        BankStatementLine.status == StatementLineStatus.UNMATCHED,
                    )
                )
            ).scalar_one()
            UNMATCHED_STATEMENT_LINES.labels(company_id=label).set(
                int(unmatched_count)
            )


# ---------------------------------------------------------------------- #
# Middleware                                                              #
# ---------------------------------------------------------------------- #


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record latency + count per (method, route, status)."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Skip scrapes themselves so scraper load doesn't distort
        # app-traffic quantiles.
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Record a synthetic 500 so crashes still increment the
            # counter — observability depends on it.
            elapsed = time.perf_counter() - start
            route = _route_template(request)
            REQUEST_LATENCY.labels(
                method=request.method, route=route, status="500"
            ).observe(elapsed)
            REQUEST_COUNT.labels(
                method=request.method, route=route, status="500"
            ).inc()
            raise

        elapsed = time.perf_counter() - start
        route = _route_template(request)
        status = str(response.status_code)
        REQUEST_LATENCY.labels(
            method=request.method, route=route, status=status
        ).observe(elapsed)
        REQUEST_COUNT.labels(
            method=request.method, route=route, status=status
        ).inc()
        return response


def _route_template(request: Request) -> str:
    """Return the Starlette route pattern, or raw path as fallback.

    ``request.scope['route']`` is populated after routing; the
    template keeps histogram label cardinality bounded even for
    UUID-heavy routes. Falls back to the raw path for 404s /
    static file hits.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) if route is not None else None
    if isinstance(path, str) and path:
        return path
    return request.url.path


# ---------------------------------------------------------------------- #
# /metrics endpoint                                                       #
# ---------------------------------------------------------------------- #


async def metrics_endpoint(request: Request) -> Response:
    """``GET /metrics`` — Prometheus text format 0.0.4."""
    try:
        await refresh_gauges()
    except Exception as exc:  # pragma: no cover — logged + served
        # Don't fail the scrape on a DB hiccup — stale gauges beat
        # an empty page that silently breaks alerting.
        _LOG.warning(
            "Gauge refresh failed, serving last known values: %s", exc
        )

    data = generate_latest(_REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def install(app: FastAPI) -> None:
    """Install middleware + register the /metrics route on the app."""
    app.add_middleware(PrometheusMiddleware)
    app.add_route("/metrics", metrics_endpoint, methods=["GET"])


__all__ = [
    "OPEN_INVOICES",
    "OVERDUE_INVOICES",
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "UNMATCHED_STATEMENT_LINES",
    "PrometheusMiddleware",
    "install",
    "metrics_endpoint",
    "refresh_gauges",
]
