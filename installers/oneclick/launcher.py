"""SAE Books one-click server launcher.

Single self-contained process that gives a non-technical user a working
SAE Books community server: double-click the app, the ledger initialises
itself (SQLite, no Postgres, no Docker), the engine API and the web UI
start on localhost, and the default browser opens on the sign-in page.

This is the SERVER — the same FastAPI engine + saebooks-web UI the docker
community bundle runs (docker-compose.community.yml), collapsed into one
native process:

    engine  (saebooks.main:app)      127.0.0.1:<api port,  default 18961>
    web UI  (saebooks_web.main:app)  127.0.0.1:<web port,  default 18960>
    gRPC    (engine sidecar)         127.0.0.1:<grpc port, default 18962>

State lives in the per-user data dir (never next to the executable, which
may be read-only):

    Linux    ~/.local/share/SAEBooks
    Windows  %LOCALAPPDATA%\\SAEBooks
    macOS    ~/Library/Application Support/SAEBooks

First run generates real random secrets into <data>/secrets.env (0600) —
the docker compose "local-only placeholder" story is not acceptable here
because the file ships to end users. Ledger = <data>/saebooks.db.

Environment overrides (all optional): SAEBOOKS_ONECLICK_DATA_DIR,
SAEBOOKS_ONECLICK_WEB_PORT, SAEBOOKS_ONECLICK_API_PORT,
SAEBOOKS_ONECLICK_NO_BROWSER=1, SAEBOOKS_BRAND (saebooks|tasur).
"""
from __future__ import annotations

import os
import secrets as _secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "SAEBooks"
DEFAULT_WEB_PORT = 18960
DEFAULT_API_PORT = 18961
DEFAULT_GRPC_PORT = 18962

DEMO_EMAIL = "you@example.com"
DEMO_PASSWORD = "change-me-now"


def data_dir() -> Path:
    override = os.environ.get("SAEBOOKS_ONECLICK_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / APP_NAME


def _splash_update(text: str) -> None:
    """Progress text on the Windows bootloader splash; no-op everywhere else.

    pyi_splash only exists inside a frozen app built with a Splash target, and
    dies with the splash window — every call is best-effort.
    """
    try:
        import pyi_splash  # type: ignore

        if pyi_splash.is_alive():
            pyi_splash.update_text(text)
    except Exception:
        pass


def _splash_close() -> None:
    try:
        import pyi_splash  # type: ignore

        pyi_splash.close()
    except Exception:
        pass


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    if _port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _existing_instance(web_port: int, api_port: int) -> bool:
    """True if a healthy SAE Books server already answers on both ports.

    Guards the double-launch case (user re-clicks during the silent first-run
    extraction, or simply forgot the server is running): reuse the running
    instance instead of silently starting a second server on fallback ports.
    """
    import httpx

    try:
        web = httpx.get(f"http://127.0.0.1:{web_port}/healthz", timeout=1.0)
        api = httpx.get(f"http://127.0.0.1:{api_port}/api/v1/healthz", timeout=1.0)
        return web.status_code == 200 and api.status_code == 200
    except Exception:
        return False


def load_or_create_secrets(ddir: Path) -> dict[str, str]:
    """Per-install random secrets, persisted so JWTs/sessions survive restarts."""
    from cryptography.fernet import Fernet

    path = ddir / "secrets.env"
    vals: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip()
    changed = False
    for key, gen in (
        ("SAEBOOKS_SECRET_KEY", lambda: _secrets.token_hex(32)),
        ("SAEBOOKS_FIELD_ENCRYPTION_KEY", lambda: Fernet.generate_key().decode()),
        ("SAEBOOKS_WEB_SECRET_KEY", lambda: _secrets.token_hex(32)),
    ):
        if not vals.get(key):
            vals[key] = gen()
            changed = True
    if changed:
        path.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
        try:
            path.chmod(0o600)
        except OSError:
            pass  # NTFS — ACLs already scope %LOCALAPPDATA% to the user
    return vals


def configure_env(ddir: Path, web_port: int, api_port: int, grpc_port: int) -> None:
    """Set the full community-edition env contract BEFORE importing saebooks.

    Mirrors docker-compose.community.yml; os.environ.setdefault everywhere so
    a power user can still override anything from the shell.
    """
    sec = load_or_create_secrets(ddir)
    db_path = (ddir / "saebooks.db").as_posix()
    env = {
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "SAEBOOKS_EDITION": "community",
        "SAEBOOKS_BIND_HOST": "127.0.0.1",
        "SAEBOOKS_GRPC_PORT": str(grpc_port),
        "SAEBOOKS_SECRET_KEY": sec["SAEBOOKS_SECRET_KEY"],
        "SAEBOOKS_FIELD_ENCRYPTION_KEY": sec["SAEBOOKS_FIELD_ENCRYPTION_KEY"],
        "SAEBOOKS_DEMO_EMAIL": DEMO_EMAIL,
        "SAEBOOKS_DEMO_PASSWORD": DEMO_PASSWORD,
        "SAEBOOKS_DEMO_COMPANY_NAME": "My Business",
        # PDF rendering: the engine POSTs document facts to the web app's
        # /internal/render, which in turn needs the latex-api XeLaTeX service.
        # One-click bundles no latex-api, so PDFs are unavailable — but wiring
        # the first hop to the in-process web app makes the failure a clean,
        # immediate 502 instead of a 120 s DNS timeout on "http://web:8080".
        "RENDER_SERVICE_URL": f"http://127.0.0.1:{web_port}",
        # web UI
        "SAEBOOKS_WEB_API_URL": f"http://127.0.0.1:{api_port}",
        "SAEBOOKS_WEB_SECRET_KEY": sec["SAEBOOKS_WEB_SECRET_KEY"],
        "SAEBOOKS_WEB_SITE_ORIGIN": (
            f"http://127.0.0.1:{web_port},http://localhost:{web_port}"
        ),
        "SAEBOOKS_BRAND": "saebooks",
        "SAEBOOKS_OAUTH_ENABLED": "false",
        "SAEBOOKS_WEBAUTHN_ENABLED": "false",
    }
    for k, v in env.items():
        os.environ.setdefault(k, v)


def init_db_and_seed(ddir: Path) -> None:
    """bootstrap_schema (the SQLite path that replaces alembic) + starter books.

    The demo seed is idempotent, but only run it while the user hasn't made the
    books their own: seeding is skipped once a marker exists so a user who
    deletes the sample company doesn't get it back on next launch.
    """
    import asyncio

    from saebooks.db import bootstrap_schema

    asyncio.run(bootstrap_schema())

    marker = ddir / ".seeded"
    if not marker.exists():
        from saebooks.cli.seed_cashbook_demo import main as seed_main

        asyncio.run(seed_main())
        marker.write_text("seeded\n")


def make_server(app_import: str, port: int):
    import uvicorn

    config = uvicorn.Config(
        app_import,
        host="127.0.0.1",
        port=port,
        workers=1,           # SQLite is single-writer
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)


def wait_healthy(url: str, timeout: float = 90.0) -> bool:
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    print("Starting SAE Books — first run can take about a minute...")
    ddir = data_dir()
    ddir.mkdir(parents=True, exist_ok=True)

    web_pref = int(os.environ.get("SAEBOOKS_ONECLICK_WEB_PORT", "0")) or DEFAULT_WEB_PORT
    api_pref = int(os.environ.get("SAEBOOKS_ONECLICK_API_PORT", "0")) or DEFAULT_API_PORT

    if not _port_free(web_pref) and _existing_instance(web_pref, api_pref):
        url = f"http://127.0.0.1:{web_pref}"
        _splash_close()
        print()
        print("SAE Books is already running — no need to start it again.")
        print(f"  Open: {url}")
        if os.environ.get("SAEBOOKS_ONECLICK_NO_BROWSER", "") != "1":
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0

    web_port = pick_port(web_pref)
    api_port = pick_port(api_pref)
    grpc_port = pick_port(DEFAULT_GRPC_PORT)
    for name, pref, got in (("web", web_pref, web_port), ("engine", api_pref, api_port)):
        if got != pref:
            print(f"Note: the usual {name} port {pref} is in use by another program — using {got} instead.")

    configure_env(ddir, web_port, api_port, grpc_port)

    print(f"SAE Books — your books live in {ddir}")
    print("Preparing the ledger (first run takes a few seconds)...")
    _splash_update("Preparing your ledger…")
    init_db_and_seed(ddir)

    _splash_update("Starting the server…")
    engine_srv = make_server("saebooks.main:app", api_port)
    web_srv = make_server("saebooks_web.main:app", web_port)

    threads = []
    for srv in (engine_srv, web_srv):
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        threads.append(t)

    if not wait_healthy(f"http://127.0.0.1:{api_port}/api/v1/healthz"):
        _splash_close()
        print("ERROR: the SAE Books engine did not start. See messages above.", file=sys.stderr)
        return 1
    if not wait_healthy(f"http://127.0.0.1:{web_port}/healthz"):
        _splash_close()
        print("ERROR: the SAE Books web interface did not start.", file=sys.stderr)
        return 1

    _splash_close()
    url = f"http://127.0.0.1:{web_port}"
    print()
    print("SAE Books is running.")
    print(f"  Open:      {url}")
    print(f"  Sign in:   {DEMO_EMAIL}")
    print(f"  Password:  {DEMO_PASSWORD}   (change it after signing in)")
    print()
    print("Keep this window open while you use SAE Books. Close it (or press")
    print("Ctrl+C) to stop the server. Your books are saved automatically.")

    if os.environ.get("SAEBOOKS_ONECLICK_NO_BROWSER", "") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping SAE Books...")
        engine_srv.should_exit = True
        web_srv.should_exit = True
        for t in threads:
            t.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
