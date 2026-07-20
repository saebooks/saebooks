"""Jurisdiction-module bootstrap — Job C registration inversion.

Design: ``~/.claude/plans/saebooks-neutral-core-strip.md`` (Job C).

Before this module existed, the core registration hubs
(``services.tax_engine`` / ``services.jurisdiction_modules``) imported
every jurisdiction package directly to register it — core→module
imports, the thing the neutral-core strip removes. Now each
``saebooks.jurisdictions.<cc>`` package SELF-registers on import (see
that package's ``__init__.py``); this module is the one place that
decides WHICH packages get imported, and it lives outside
``services``/``models`` on purpose — the core must never name a
country, but *something* app-level has to choose the boot-time set.

``ensure_loaded()`` is idempotent (one-shot flag) and is called lazily
from the six per-capability registry readers
(``tax_engine.get_engine``/``resolve_engine``,
``jurisdiction_modules.get_descriptor``/``list_descriptors``, and
``payroll``'s ``_ensure_modules_registered``) plus explicitly from
``tests/conftest.py`` and ``main.py``'s ``lifespan()`` so the
registration always happens exactly once, on first need, however the
app is entered.

EE's tax engine is core-native (``services/tax_engine/ee.py``), but the
``jurisdictions/ee`` package DOES exist and self-registers on import: its
``ee/default`` chart-template applier and its AR/AP control-account
convention codes (see ``jurisdictions/ee/__init__.py``). So ``"ee"`` is
listed in ``_MODULE_PATHS`` — ``ensure_loaded()`` imports the package so
those seams are present before ``services.templates`` /
``services.control_accounts`` read them. The EE package is import-light
(it registers lazy factories only), so importing it at boot does not
pull the runtime-env-dependent chart/identifier modules.
"""
from __future__ import annotations

import importlib

from saebooks.config import settings

_MODULE_PATHS: dict[str, str] = {
    "au": "saebooks.jurisdictions.au",
    "nz": "saebooks.jurisdictions.nz",
    "uk": "saebooks.jurisdictions.uk",
    "lt": "saebooks.jurisdictions.lt",
    "lv": "saebooks.jurisdictions.lv",
    "ee": "saebooks.jurisdictions.ee",
}

# One-shot guard. Set BEFORE the import loop (not after, unlike
# payroll's ``_modules_loaded`` idiom) so a jurisdiction package whose
# import path re-enters ``ensure_loaded()`` (directly or transitively)
# sees the guard already tripped instead of recursing into the loop
# again.
_loaded = False


def ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for code in settings.enabled_jurisdictions_set:
        path = _MODULE_PATHS.get(code)
        if path:
            importlib.import_module(path)
