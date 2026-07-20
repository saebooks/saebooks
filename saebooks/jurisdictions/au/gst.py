"""Deprecated shim — use ``saebooks.jurisdictions.au.tax`` instead.

This module re-exports the GST auto-posting and BAS settlement helpers
that moved into the per-jurisdiction tax engine in M0 (and into the AU
jurisdiction module in jmod Phase 2). Importing it emits a
:class:`DeprecationWarning` so callers know to migrate.

Drop scheduled at M1 entry — all internal callers should be migrated
by then. Until then this shim keeps every old import path working
unchanged so the refactor stays risk-free.
"""
from __future__ import annotations

import warnings

# Emit deprecation at import time. `stacklevel=2` points at the caller
# (the `import saebooks.jurisdictions.au.gst` line), not at this module.
warnings.warn(
    "saebooks.jurisdictions.au.gst is deprecated; "
    "import from saebooks.jurisdictions.au.tax instead. "
    "This shim is dropped at M1.",
    DeprecationWarning,
    stacklevel=2,
)

from saebooks.jurisdictions.au.tax import (  # noqa: E402
    TaxConfigError,
    auto_post_gst_lines,
    is_auto_post_enabled,
    settle_bas,
    validate_gst_account_settings,
)

__all__ = [
    "TaxConfigError",
    "auto_post_gst_lines",
    "is_auto_post_enabled",
    "settle_bas",
    "validate_gst_account_settings",
]
