"""Capture background worker (#32 step 5).

An asyncio loop that runs the existing CLI jobs ``sync-feeds`` and
``reconcile-feeds`` (``saebooks.cli._sync_feeds`` / ``_reconcile_feeds``) on an
interval, controlled by ``CAPTURE_SYNC_INTERVAL_MINUTES``:

* ``0`` (or unset) — **DEFAULT** — the worker idles and only logs a
  heartbeat; it does NOT run any job. No scheduler currently invokes
  ``sync-feeds`` anywhere on the host, so the worker is forward-provisioning,
  not a live migration. Standing the container up with the default env is a
  no-op beyond the heartbeat.
* a positive integer ``N`` — run one ``sync-feeds`` + ``reconcile-feeds``
  iteration, then sleep ``N`` minutes, repeat.

RLS: the sync job runs through ``saebooks.cli._sync_feeds`` which enforces the
NOBYPASSRLS design (``_assert_not_bypass`` + the strict ``saebooks_app``
session factory). The worker does NOT pass ``allow_bypass`` — a
BYPASSRLS/superuser DB role makes the worker refuse to sync, by design.

Run with ``MODE=worker python -m capture_app`` (dispatched in
``capture_app.__main__``).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from saebooks.cli import _reconcile_feeds, _sync_feeds
from saebooks.config import Settings
from saebooks.config import settings as _default_settings

logger = logging.getLogger("saebooks.capture.worker")

# How often to emit a heartbeat when idle (interval <= 0). Long enough not to
# spam logs, short enough to prove liveness. Overridable by tests via the
# ``sleep``/``max_iterations`` injection points on ``run``.
_HEARTBEAT_SECONDS = 300.0


async def run_iteration(*, allow_bypass: bool = False) -> dict[str, int]:
    """Run one sync-feeds + reconcile-feeds pass across all active feeds.

    Returns the two job exit codes so a caller/test can assert on outcomes.
    Exceptions from either job propagate to the caller; the loop in ``run``
    catches and logs them so one bad iteration doesn't kill the worker.
    """
    sync_rc = await _sync_feeds(None, allow_bypass=allow_bypass)
    reconcile_rc = await _reconcile_feeds(None)
    return {"sync_rc": sync_rc, "reconcile_rc": reconcile_rc}


async def run(
    *,
    settings: Settings | None = None,
    max_iterations: int | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Run the worker loop.

    Parameters
    ----------
    settings:
        Optional ``Settings`` override (falls back to the singleton).
    max_iterations:
        Stop after this many loop iterations. ``None`` = run forever (the
        production path). Tests pass a small integer to bound the loop.
    sleep:
        Injectable async sleep (defaults to ``asyncio.sleep``) so tests can
        run the loop without real delays.
    """
    effective = settings if settings is not None else _default_settings
    sleep_fn = sleep if sleep is not None else asyncio.sleep

    interval_minutes = effective.capture_sync_interval_minutes
    idle = interval_minutes <= 0

    if idle:
        logger.info(
            "capture-worker: starting in IDLE mode "
            "(CAPTURE_SYNC_INTERVAL_MINUTES=%s) — heartbeat only, no sync scheduled",
            interval_minutes,
        )
    else:
        logger.info(
            "capture-worker: starting — sync+reconcile every %d minute(s)",
            interval_minutes,
        )

    count = 0
    while max_iterations is None or count < max_iterations:
        if idle:
            logger.info(
                "capture-worker: idle heartbeat "
                "(CAPTURE_SYNC_INTERVAL_MINUTES=%s); no jobs run",
                interval_minutes,
            )
        else:
            logger.info("capture-worker: running sync+reconcile iteration %d", count + 1)
            try:
                outcome = await run_iteration()
                logger.info(
                    "capture-worker: iteration %d done (sync_rc=%s reconcile_rc=%s)",
                    count + 1,
                    outcome["sync_rc"],
                    outcome["reconcile_rc"],
                )
            except Exception:
                logger.exception("capture-worker: iteration %d failed", count + 1)

        count += 1
        if max_iterations is not None and count >= max_iterations:
            break

        delay = _HEARTBEAT_SECONDS if idle else 60.0 * interval_minutes
        await sleep_fn(delay)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
