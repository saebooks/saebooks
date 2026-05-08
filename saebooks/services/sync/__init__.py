"""Bidirectional accounting-package sync services.

Build #9 (Enterprise tier) — see ``~/.claude/plans/saebooks-accounting-sync.md``
for the architecture decision log. Three providers, three sub-packages:

* ``xero/``  — first adapter (cleanest OAuth, single API surface).
* ``myob/``  — to come (regional endpoints, RowVersion not ETag).
* ``qbo/``   — to come (re-shape from books-sauer-migration).

Common types (errors, sync-state helpers) live at this package level
so all three providers share one shape.
"""
