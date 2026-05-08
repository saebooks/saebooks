# Changelog

All notable changes to SAE Books will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-05-08

### Added

- **Purchase orders.** Full lifecycle (`DRAFT → OPEN → PARTIAL/RECEIVED → CLOSED`,
  with `CANCELLED` terminal from DRAFT/OPEN/PARTIAL). Convert-to-bill produces a
  draft Bill from outstanding line quantities; multi-receipt billing supported via
  per-line `received_qty`. No GL impact at the PO layer — the financial event is
  the converted bill.
- **Proration previews.** Three primitives plus a deferred-revenue wrapper:
  per-line date-range, first-period sign-up, and mid-period plan change. All math
  at full `Decimal` precision; quantise once at the end.
- New endpoints under `/api/v1/purchase_orders` and `/api/v1/proration`.
- Alembic migration `0094_purchase_orders`.

## [0.1.0] - 2026-05-08

Initial public alpha.
