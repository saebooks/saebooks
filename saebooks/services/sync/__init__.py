"""Bidirectional accounting-package sync services.

Enterprise tier (``FLAG_ACCOUNTING_SYNC`` + a per-provider sub-flag —
see ``saebooks/services/features.py``). One sub-package per provider:

* ``xero/`` — first (and, as of this port, only) adapter implementation.

Common types (errors, sync-state helpers) live at this package level
so future providers share one shape.
"""
