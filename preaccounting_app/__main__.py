"""Single-image entrypoint dispatcher for the pre-accounting module (#32 step 4).

One container image, one role for now, selected by the ``MODE`` env var so the
image shares the capture / platform deploy ergonomics and can grow a worker
role later without a new entrypoint:

* ``MODE=web`` (default) — serve the FastAPI app on ``0.0.0.0:8080``
  (equivalent to ``uvicorn preaccounting_app.main:app``).

Usage in the image::

    CMD ["python", "-m", "preaccounting_app"]
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    mode = os.environ.get("MODE", "web").strip().lower()
    if mode == "web":
        import uvicorn

        port = int(os.environ.get("PORT", "8080"))
        uvicorn.run("preaccounting_app.main:app", host="0.0.0.0", port=port)
    else:
        sys.stderr.write(
            f"preaccounting_app: unknown MODE={mode!r} (expected 'web')\n"
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
