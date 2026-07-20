"""App-level bootstrap — NOT core.

``services/`` and ``models/`` (the core) never import from here at
module level; this package is the sanctioned place for wiring that
needs to know about jurisdiction bolt-on modules, config, or other
boot-time concerns the core itself must stay ignorant of. See
``saebooks.bootstrap.jurisdictions`` for the registration-inversion
entry point (Job C, ``~/.claude/plans/saebooks-neutral-core-strip.md``).
"""
from __future__ import annotations
