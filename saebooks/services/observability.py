"""Structured logging + Sentry wiring — both runtime-opt-in.

Two independent features that share this module because they're both
"observability plumbing":

* **Structured JSON logs** — when ``SAEBOOKS_LOG_JSON=1``, every record
  on the root logger is serialised to a one-line JSON object. Shape is
  Loki/ELK-friendly: ``{ "ts", "level", "logger", "msg", ... }``. When
  disabled (the default), we leave the standard text format alone.

* **Sentry** — when ``SENTRY_DSN`` is set, ``sentry_sdk.init`` installs
  the FastAPI + Starlette integrations so unhandled 5xx exceptions
  propagate upstream automatically. When empty, this module is a no-op
  (no network, no global state change) — Community users don't ship
  data to our servers by default.

Both features share one entry point ``configure()`` called from
``saebooks.main.create_app`` exactly once. Safe to call twice — the
JSON formatter detects its own tag and refuses to double-attach, and
``sentry_sdk.init`` is idempotent by design.
"""
from __future__ import annotations

import logging
from typing import Any

from saebooks.config import Settings
from saebooks.config import settings as default_settings

_LOG = logging.getLogger("saebooks.observability")

# A sentinel so we don't replace the formatter twice if create_app is
# re-invoked in tests — the second call would wrap our own JSON output
# in another JSON layer, producing nonsense.
_JSON_TAG = "_saebooks_json_attached"


# ---------------------------------------------------------------------- #
# JSON logging                                                            #
# ---------------------------------------------------------------------- #


def _make_json_formatter() -> logging.Formatter:
    """Build the JSON formatter with our canonical field set.

    ``pythonjsonlogger`` 4.x exposes the formatter at
    ``pythonjsonlogger.json.JsonFormatter``. The format string is a
    *field spec* — each ``%(x)s`` token becomes a top-level key in the
    output JSON, and extras passed via ``logger.info("msg", extra={...})``
    are merged in alongside.
    """
    from pythonjsonlogger.json import JsonFormatter

    return JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={
            "asctime": "ts",
            "levelname": "level",
            "name": "logger",
            "message": "msg",
        },
    )


def configure_json_logging(*, force: bool = False) -> bool:
    """Swap every root handler's formatter to JSON.

    Returns ``True`` if the swap happened, ``False`` if logging was
    already configured for JSON (idempotency) or there are no root
    handlers to reformat.

    ``force=True`` re-attaches even if already tagged — tests use this
    to recover the formatter after another test replaced the root
    handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        # basicConfig hasn't run yet; install a StreamHandler so we
        # have somewhere to hang the formatter.
        logging.basicConfig(level=root.level or logging.INFO)

    fmt = _make_json_formatter()
    changed = False
    for handler in root.handlers:
        if getattr(handler, _JSON_TAG, False) and not force:
            continue
        handler.setFormatter(fmt)
        setattr(handler, _JSON_TAG, True)
        changed = True
    return changed


# ---------------------------------------------------------------------- #
# Sentry                                                                  #
# ---------------------------------------------------------------------- #


def configure_sentry(dsn: str, *, environment: str = "production") -> bool:
    """Init Sentry if ``dsn`` is non-empty.

    Returns ``True`` if init ran, ``False`` on empty DSN.

    The FastAPI integration is auto-loaded by sentry-sdk when FastAPI
    is importable — we don't need to pass it explicitly. We DO set
    ``send_default_pii=False`` so request bodies / cookies aren't
    shipped upstream by default; operators can flip it on per-env.
    """
    if not dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        # Keep traces off by default — they're noisy and cost quota.
        # Operators can set traces_sample_rate via env + subclass later.
        traces_sample_rate=0.0,
    )
    return True


# ---------------------------------------------------------------------- #
# Single entry point                                                      #
# ---------------------------------------------------------------------- #


def configure(app_settings: Settings | None = None) -> dict[str, Any]:
    """Install both observability features once, per the live settings.

    Returns a small status dict so callers (and tests) can assert on
    what happened without parsing log output.
    """
    s = app_settings or default_settings
    json_active = False
    sentry_active = False

    if s.log_json:
        json_active = configure_json_logging()
        if json_active:
            _LOG.info("json_logging_enabled")

    if s.sentry_dsn:
        sentry_active = configure_sentry(
            s.sentry_dsn, environment=s.edition
        )
        if sentry_active:
            _LOG.info("sentry_enabled")

    return {"json_logging": json_active, "sentry": sentry_active}


__all__ = [
    "configure",
    "configure_json_logging",
    "configure_sentry",
]
