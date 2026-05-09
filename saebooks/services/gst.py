"""DEPRECATED — kept as a shim re-exporting the AU tax engine.

The GST auto-posting + BAS-settlement helpers moved into the per-
jurisdiction tax engine package at
``saebooks.services.tax_engine.au`` (M0 multi-jurisdiction refactor).
This module re-exports the public names so existing callers
(``from saebooks.services import gst``) continue to work for one
release.

Drops at M1 entry. Migrate callers to::

    from saebooks.services.tax_engine.au import (
        auto_post_gst_lines,
        is_auto_post_enabled,
        settle_bas,
    )

or, preferably, use the protocol::

    from saebooks.services.tax_engine import get_engine
    engine = get_engine("AU")
"""
from __future__ import annotations

import warnings

from saebooks.services.tax_engine.au import (  # noqa: F401  (re-export)
    _get_gst_account,
    _INPUT_TYPES,
    _OUTPUT_TYPES,
    auto_post_gst_lines,
    is_auto_post_enabled,
    settle_bas,
)

warnings.warn(
    "saebooks.services.gst is deprecated; import from "
    "saebooks.services.tax_engine.au or use "
    "saebooks.services.tax_engine.get_engine('AU') instead. "
    "This shim is removed at M1.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "_INPUT_TYPES",
    "_OUTPUT_TYPES",
    "_get_gst_account",
    "auto_post_gst_lines",
    "is_auto_post_enabled",
    "settle_bas",
]
