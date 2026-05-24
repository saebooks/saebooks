"""Deprecated shim — use ``saebooks.services.tax_engine.au`` instead.

This module re-exports the BAS report builder + dataclasses that
moved into the per-jurisdiction tax engine in M0. Importing it emits
a :class:`DeprecationWarning` so callers know to migrate.

Drop scheduled at M1 entry — all internal callers should be migrated
by then. Until then this shim keeps every old import path working
unchanged so the refactor stays risk-free.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "saebooks.services.bas is deprecated; "
    "import from saebooks.services.tax_engine.au instead. "
    "This shim is dropped at M1.",
    DeprecationWarning,
    stacklevel=2,
)

from saebooks.services.tax_engine.au import (  # noqa: E402
    BASLine,
    BASReport,
    bas_report,
)

__all__ = [
    "BASLine",
    "BASReport",
    "bas_report",
]
