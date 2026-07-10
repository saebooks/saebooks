"""Single-image entrypoint dispatcher for the capture module (#32 step 5).

One container image, two roles, selected by the ``MODE`` env var:

* ``MODE=web`` (default) — serve the FastAPI app on ``0.0.0.0:8080``
  (equivalent to ``uvicorn capture_app.main:app``).
* ``MODE=worker`` — run the background sync/reconcile loop
  (``capture_app.worker.main``).

Usage in the image::

    CMD ["python", "-m", "capture_app"]
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    mode = os.environ.get("MODE", "web").strip().lower()
    if mode == "worker":
        from capture_app.worker import main as worker_main

        worker_main()
    elif mode == "web":
        import uvicorn

        port = int(os.environ.get("PORT", "8080"))
        uvicorn.run("capture_app.main:app", host="0.0.0.0", port=port)
    else:
        sys.stderr.write(
            f"capture_app: unknown MODE={mode!r} (expected 'web' or 'worker')\n"
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
