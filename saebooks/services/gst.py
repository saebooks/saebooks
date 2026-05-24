"""Deprecated shim — use ``saebooks.services.tax_engine.au`` instead.

This module re-exports the GST auto-posting and BAS settlement helpers
that moved into the per-jurisdiction tax engine in M0. Importing it
emits a :class:`DeprecationWarning` so callers know to migrate.

Drop scheduled at M1 entry — all internal callers should be migrated
by then. Until then this shim keeps every old import path working
unchanged so the refactor stays risk-free.
"""
from __future__ import annotations

import warnings

# Emit deprecation at import time. `stacklevel=2` points at the caller
# (the `import saebooks.services.gst` line), not at this module.
warnings.warn(
    "saebooks.services.gst is deprecated; "
    "import from saebooks.services.tax_engine.au instead. "
    "This shim is dropped at M1.",
    DeprecationWarning,
    stacklevel=2,
)

from saebooks.services.tax_engine.au import (  # noqa: E402
    auto_post_gst_lines,
    is_auto_post_enabled,
    settle_bas,
)

__all__ = [
    "auto_post_gst_lines",
    "is_auto_post_enabled",
    "settle_bas",
]
