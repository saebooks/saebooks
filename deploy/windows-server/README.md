# SAE Books — Windows all-in-one installer

> **Status:** scaffold only. The pre-built `.exe` is **not** part of
> the v0.1 release; it will be attached to v0.1.1. Until then,
> Linux/Docker is the recommended install path.

This directory contains the recipe for building a single
self-contained Windows installer (`SAEBooks-Setup-vX.Y.Z.exe`) that
bundles:

* Python 3.12 embeddable distribution
* Portable PostgreSQL 16 binaries (`postgres`, `pg_ctl`, `initdb`,
  `psql`)
* The `saebooks` API and `saebooks-web` web UI source trees, with
  their dependencies installed into the embedded Python
* A Windows service shim that starts Postgres + API + Web on boot
* A first-run wizard that calls `initdb`, runs Alembic migrations,
  generates secrets, and opens a browser to `http://localhost:8080`

The build runs on a Windows host (Inno Setup is Windows-only).

## Prerequisites (on a Windows 10/11 machine)

* [Inno Setup 6](https://jrsoftware.org/isdl.php) — installs `iscc.exe`
* PowerShell 7+ (built into Windows 11, install on Windows 10)
* Internet access — `build.ps1` downloads Python and Postgres binaries

## Build

```powershell
# from the saebooks repo root
cd deploy\windows-server
.\build.ps1            # downloads binaries + lays out staging\
iscc setup.iss         # produces dist\SAEBooks-Setup-vX.Y.Z.exe
```

Output: `deploy\windows-server\dist\SAEBooks-Setup-v0.1.exe`.

## Files

| File | Purpose |
|---|---|
| `build.ps1` | Downloads embedded Python + Portable Postgres, copies saebooks + saebooks-web sources, builds the staging directory consumed by Inno Setup. |
| `setup.iss` | Inno Setup script. Defines the installer UI, install paths, services, shortcuts, and uninstall behaviour. |
| `first-run.ps1` | Runs once on first launch — `initdb`, `alembic upgrade head`, generates secrets into `%ProgramData%\SAEBooks\.env`, registers Windows services. |
| `service-postgres.xml`, `service-api.xml`, `service-web.xml` | WinSW (a single-file service wrapper) descriptors for the three services. |

## Install path layout

```
C:\Program Files\SAEBooks\           ; binaries (read-only after install)
  python\                            ; embedded Python 3.12
  postgres\                          ; pgsql binaries
  saebooks\                          ; API source
  saebooks-web\                      ; web source
  bin\                               ; service shims, first-run.ps1

C:\ProgramData\SAEBooks\             ; data (writable, per-machine)
  pgdata\                            ; Postgres data directory
  logs\                              ; service logs
  backups\                           ; pg_dump output
  .env                               ; generated secrets (mode-restricted)
```

## Why this isn't shipped at v0.1

Two reasons:

1. The build chain requires a Windows host that the maintainer
   doesn't currently have automated; the pre-built `.exe` would be
   built by hand for the first few releases and we'd rather not ship
   a binary that isn't rebuildable from the public source.
2. None of the bundled service-wrapper paths (Postgres + uvicorn under
   WinSW) have been exercised end-to-end on a clean Windows VM.

Until v0.1.1 lands, Windows users with Docker Desktop should follow
the [Linux/Docker Quickstart](../../README.md). A native installer
without Docker arrives shortly after.
