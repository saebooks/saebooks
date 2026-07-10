"""Theme catalogue — the CHARTER §12.1 "themes" allow-list.

Per CHARTER §6.1/§6.2/§12.1 ("Community ... stock theme only" / Offline
"All themes (default, MYOB Classic, and any others we ship)" / matrix row
"All themes (MYOB Classic, SS, TT, etc.)"), theme SELECTION beyond the
single free default is an Offline+ feature, gated by ``FLAG_THEMES``. The
engine's job is narrow and stays that way:

* validate a ``preferred_theme`` identifier against a canonical allow-list
  (this module);
* gate a request that *sets* a non-default theme behind ``FLAG_THEMES``
  (``api/v1/users.py``, inline — mirrors the multi_currency /
  non-base-currency-document pattern);
* serve the catalogue at ``GET /api/v1/themes`` (``api/v1/themes.py``,
  route-gated — Community can't enumerate paid-tier theme names any more
  than it can browse any other paid-tier route).

The actual per-theme CSS bundle lives in saebooks-web (out of scope here
— see ``alembic/versions/0029_user_preferred_theme.py``, which already
anticipated this module by name: "Validated against
``services.theme.ACTIVE_THEMES`` at write time").

CHARTER abbreviations
----------------------
CHARTER.md §12.1 lists "MYOB Classic, SS, TT, etc." without ever spelling
out SS / TT anywhere else in the codebase, git history, or saebooks-web
(checked as of Wave B, 2026-07-10). Expanded here as a best-effort,
sensible guess — "Solarized" and "Terminal" — because the identifiers
stored in ``users.preferred_theme`` need to exist as *something* today.
If Richard's actual intended set differs, this module is the single
place to fix it (the DB stores only the ``id`` string, so relabelling is
a one-line change with no migration). Flagged explicitly in the Wave B
report rather than silently guessed.
"""
from __future__ import annotations

from dataclasses import dataclass

# The default is always available, at every tier including Community
# (CHARTER §6.1: "stock theme only"). Every other catalogue entry
# requires FLAG_THEMES (Offline+).
DEFAULT_THEME_ID = "default"


@dataclass(frozen=True, slots=True)
class ThemeDef:
    id: str
    label: str


# CHARTER §12.1 matrix row: "All themes (MYOB Classic, SS, TT, etc.)".
# "SS" / "TT" are unresolved abbreviations -- expanded as a documented
# guess (Solarized / Terminal), see module docstring.
THEME_CATALOG: tuple[ThemeDef, ...] = (
    ThemeDef(id=DEFAULT_THEME_ID, label="Default"),
    ThemeDef(id="myob_classic", label="MYOB Classic"),
    ThemeDef(id="solarized", label="Solarized"),  # CHARTER "SS" -- guess
    ThemeDef(id="terminal", label="Terminal"),  # CHARTER "TT" -- guess
)

# The canonical allow-list. Referenced by name from
# alembic/versions/0029_user_preferred_theme.py's docstring.
ACTIVE_THEMES: frozenset[str] = frozenset(t.id for t in THEME_CATALOG)


def is_valid_theme_id(theme_id: str | None) -> bool:
    """``None`` (inherit the server-wide theme) is always valid; any
    non-empty string must be a member of ``ACTIVE_THEMES``."""
    if theme_id is None:
        return True
    return theme_id in ACTIVE_THEMES


def theme_catalog_payload() -> list[dict[str, str]]:
    """JSON-shaped catalogue for ``GET /api/v1/themes``."""
    return [{"id": t.id, "label": t.label} for t in THEME_CATALOG]
