# PyInstaller spec — SAE Books one-click community server.
#
# Build via build-linux.sh / build-windows.ps1 / build-macos.sh, which
# prepare the venv (engine + saebooks-web installed, grpc stubs generated)
# and set SAEBOOKS_WEB_REPO to the saebooks-web checkout whose top-level
# templates/ and static/ directories are bundled at the archive root —
# saebooks_web resolves them as Path(__file__).parent.parent, which inside
# a frozen app is sys._MEIPASS.
#
# One binary, console mode: the console window doubles as the server's
# "keep me open" surface and shows the sign-in details.

import os
import sys

# collect_submodules() IMPORTS every module (in a child that inherits this
# environment) and silently drops any that raise. saebooks.db raises without a
# DATABASE_URL, which would silently exclude the entire db/session layer from
# the bundle — give the import pass a throwaway sqlite config.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SAEBOOKS_ENV", "dev")
os.environ.setdefault("SAEBOOKS_EDITION", "community")

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

WEB_REPO = os.environ.get("SAEBOOKS_WEB_REPO")
if not WEB_REPO or not os.path.isdir(os.path.join(WEB_REPO, "templates")):
    raise SystemExit(
        "SAEBOOKS_WEB_REPO must point at a saebooks-web checkout "
        "(templates/ + static/ at its top level)"
    )

datas = (
    collect_data_files("saebooks")          # seeds/, assets/, proto/
    + collect_data_files("saebooks_web")    # i18n locales, data/eid
    + [
        (os.path.join(WEB_REPO, "templates"), "templates"),
        (os.path.join(WEB_REPO, "static"), "static"),
    ]
)

# Version metadata read at runtime via importlib.metadata by fastapi/mcp/etc.
for dist in ("saebooks", "saebooks-web", "mcp", "fastapi", "connecpy"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

hiddenimports = (
    collect_submodules("saebooks")          # models/api/jurisdictions load via importlib
    + collect_submodules("saebooks_web")
    + collect_submodules("uvicorn")
    # mcp.cli sys.exit()s on import when its optional CLI deps are absent
    + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
    + [
        "aiosqlite",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "apscheduler.schedulers.asyncio",
    ]
)

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # build-time only / not used by the community server
        "grpc_tools",
        "tkinter",
        "matplotlib",
        "pytest",
    ]
    # launcher.py imports pyi_splash; on non-Windows builds (no Splash target)
    # PyInstaller would still bundle the module and its runtime hook, which
    # prints a KeyError traceback into the user-facing console at every start.
    + (["pyi_splash"] if sys.platform != "win32" else []),
    noarchive=False,
)

pyz = PYZ(a.pure)

# Windows only: the onefile bootloader extracts for ~45 s with NO window at all
# (observed on the fresh-VM test 2026-07-22 — users double-click again). The
# bootloader-drawn splash is the only feedback possible during that phase;
# launcher.py updates and closes it via pyi_splash. Not macOS (unsupported by
# PyInstaller) and not Linux (headless installs would have no display; the
# console banner covers the much shorter extraction there).
splash = None
if sys.platform == "win32":
    splash = Splash(
        "splash.png",
        binaries=a.binaries,
        datas=a.datas,
        text_pos=(32, 180),
        text_size=11,
        text_color="#a9b1bd",
        text_default="Starting SAE Books — first run unpacks, about a minute…",
        minify_script=True,
        always_on_top=False,
    )

exe_items = [pyz, a.scripts]
if splash is not None:
    exe_items += [splash, splash.binaries]
exe_items += [a.binaries, a.datas]

exe = EXE(
    *exe_items,
    [],
    name="SAEBooks",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)
