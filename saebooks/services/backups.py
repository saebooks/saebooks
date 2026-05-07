"""Backups — read-only view of the pg_dump output directory.

This module does NOT run pg_dump. Backups are produced by an external
systemd timer (see ops/systemd/saebooks-backup.timer) which shells out
to scripts/backup.sh. That script writes:

    $SAEBOOKS_BACKUP_DIR/saebooks-YYYYMMDD-HHMMSS.sql.gz
    $SAEBOOKS_BACKUP_DIR/backups.jsonl           (append-only status log)
    $SAEBOOKS_BACKUP_DIR/restore-tests.jsonl     (append-only test log)

Why? Keeping the backup process out of the web app:
  * pg_dump lives in the postgres container, not the app container
  * a backup job that depends on the app being healthy can't restore
    the app when it's broken — exactly when you need it most
  * systemd timers are resumable, persistent, and observable via journalctl

The web tier just lists what's there and reads the status logs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BACKUP_DIR = Path(os.environ.get("SAEBOOKS_BACKUP_DIR_IN_CONTAINER", "/app/backups"))
BACKUP_LOG = BACKUP_DIR / "backups.jsonl"
RESTORE_LOG = BACKUP_DIR / "restore-tests.jsonl"


@dataclass
class DumpFile:
    name: str
    size_bytes: int
    mtime: datetime

    @property
    def size_human(self) -> str:
        return _human_bytes(self.size_bytes)


@dataclass
class BackupRun:
    ts: datetime
    status: str
    file: str
    size_bytes: int
    duration_s: int
    error: str

    @property
    def size_human(self) -> str:
        return _human_bytes(self.size_bytes)

    @property
    def ok(self) -> bool:
        return self.status == "OK"


@dataclass
class RestoreTestRun:
    ts: datetime
    status: str
    dump: str
    duration_s: int
    details: str

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def _human_bytes(n: int) -> str:
    """Format a byte count as '1.2 MiB' etc. Binary (1024-based)."""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if x < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(x)} B"
            return f"{x:.1f} {unit}"
        x /= 1024.0
    return f"{x:.1f} PiB"


def list_dumps() -> list[DumpFile]:
    """Return all .sql.gz dumps in the backup dir, newest first."""
    if not BACKUP_DIR.exists():
        return []
    out: list[DumpFile] = []
    for p in BACKUP_DIR.glob("saebooks-*.sql.gz"):
        try:
            st = p.stat()
            out.append(
                DumpFile(
                    name=p.name,
                    size_bytes=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime),
                )
            )
        except OSError:
            continue
    out.sort(key=lambda d: d.mtime, reverse=True)
    return out


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    """Read the last `limit` lines of a JSONL file. Best-effort."""
    if not path.exists():
        return []
    try:
        # Small files — just read all lines. These grow slowly
        # (one line per day for backups, one per week for tests).
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out.reverse()  # newest first
        return out
    except OSError:
        return []


def _parse_ts(s: str) -> datetime:
    """Parse an ISO-format timestamp, tolerating timezone suffixes."""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min


def recent_backup_runs(limit: int = 30) -> list[BackupRun]:
    raw = _tail_jsonl(BACKUP_LOG, limit)
    out: list[BackupRun] = []
    for r in raw:
        out.append(
            BackupRun(
                ts=_parse_ts(r.get("ts", "")),
                status=str(r.get("status", "")),
                file=str(r.get("file", "")),
                size_bytes=int(r.get("size_bytes", 0) or 0),
                duration_s=int(r.get("duration_s", 0) or 0),
                error=str(r.get("error", "")),
            )
        )
    return out


def recent_restore_tests(limit: int = 20) -> list[RestoreTestRun]:
    raw = _tail_jsonl(RESTORE_LOG, limit)
    out: list[RestoreTestRun] = []
    for r in raw:
        out.append(
            RestoreTestRun(
                ts=_parse_ts(r.get("ts", "")),
                status=str(r.get("status", "")),
                dump=str(r.get("dump", "")),
                duration_s=int(r.get("duration_s", 0) or 0),
                details=str(r.get("details", "")),
            )
        )
    return out


def summary() -> dict[str, object]:
    """Return a summary suitable for dashboard display."""
    dumps = list_dumps()
    runs = recent_backup_runs(limit=1)
    tests = recent_restore_tests(limit=1)
    total_bytes = sum(d.size_bytes for d in dumps)
    last_run = runs[0] if runs else None
    last_test = tests[0] if tests else None
    return {
        "dump_count": len(dumps),
        "total_size_bytes": total_bytes,
        "total_size_human": _human_bytes(total_bytes) if total_bytes else "0 B",
        "latest_dump": dumps[0] if dumps else None,
        "last_run": last_run,
        "last_test": last_test,
        "backup_dir": str(BACKUP_DIR),
    }
