"""DEPRECATED — kept as a shim re-exporting the AU BAS report.

The BAS report builder moved into the per-jurisdiction tax engine
package at ``saebooks.services.tax_engine.au`` (M0 multi-
jurisdiction refactor). This module re-exports the public names so
existing callers (``from saebooks.services import bas``) continue
to work for one release.

Drops at M1 entry. Migrate callers to::

    from saebooks.services.tax_engine.au import (
        BASLine,
        BASReport,
        bas_report,
    )
"""
from __future__ import annotations

import warnings

from saebooks.services.tax_engine.au import (  # noqa: F401  (re-export)
    BASLine,
    BASReport,
    bas_report,
)

warnings.warn(
    "saebooks.services.bas is deprecated; import from "
    "saebooks.services.tax_engine.au instead. "
    "This shim is removed at M1.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["BASLine", "BASReport", "bas_report"]
