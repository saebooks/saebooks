"""Backup destination push — where a completed, encrypted export goes
after it's staged locally (planned-modules Wave E).

Two destination kinds, per Richard's decision 6 ("Configurable
destination — propose rclone remotes + local path"):

* ``local_path`` — REAL, built here. Copies the already-encrypted
  ciphertext artifact to an operator-configured local path under
  ``settings.scheduled_backup_local_dest_root``. Low risk (pure
  filesystem copy, no external process/network call) and immediately
  useful (NAS mount, another local volume).

* ``rclone_remote`` — STUBBED extension point. Config validation
  accepts the shape (``{"remote": "<rclone-remote-name>",
  "path": "<dest-path>"}``) so a tenant CAN save an rclone destination
  today, but ``push()`` raises ``RemotePushNotImplementedError`` rather
  than shelling out to the ``rclone`` binary. Actually invoking rclone
  from inside the API process is a real design decision (subprocess
  lifetime/timeout, where rclone's own config+credentials live inside
  the container, retry/backoff on a flaky remote, streaming a
  potentially large ciphertext without buffering it twice) that Wave E
  deliberately does not build — per the task guardrail, this is
  "too big to fully build" for this wave. The extension point is this
  module: implement ``RcloneRemoteDestination.push`` and nothing else
  in the calling code needs to change (``services/scheduled_backups.py``
  already threads ``remote_push_status`` through
  ``ScheduledBackupRun`` for exactly this).

Neither destination is ever handed the plaintext export or the
client's passphrase — both destinations receive the CIPHERTEXT artifact
path only, consistent with the "SAE's responsibility ends at the
encrypted export" liability boundary.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from saebooks.config import Settings
from saebooks.config import settings as _default_settings


class DestinationConfigError(ValueError):
    """Raised for a malformed/unsafe destination_params shape."""


class RemotePushNotImplementedError(NotImplementedError):
    """Raised by the rclone_remote stub — see module docstring."""


@dataclass(frozen=True, slots=True)
class PushResult:
    status: str  # 'success' | 'stubbed_not_implemented' | 'failed'
    detail: str = ""


class Destination(Protocol):
    def validate(self, params: dict[str, Any]) -> None: ...
    def push(self, artifact_path: Path, params: dict[str, Any]) -> PushResult: ...


class LocalPathDestination:
    """Real: copies the ciphertext artifact under a configured root.

    ``params`` shape: ``{"relative_path": "<path under the root>"}``.
    An absolute path, or one that escapes the root via ``..`` segments,
    is rejected at ``validate()`` time — a tenant's destination config
    must never be able to write outside the operator-designated root.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else _default_settings

    def _resolve(self, params: dict[str, Any]) -> Path:
        rel = params.get("relative_path")
        if not rel or not isinstance(rel, str):
            raise DestinationConfigError(
                "local_path destination requires a non-empty 'relative_path' string"
            )
        root = Path(self._settings.scheduled_backup_local_dest_root).resolve()
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise DestinationConfigError(
                f"relative_path {rel!r} escapes the configured local "
                "destination root — rejected"
            ) from exc
        return candidate

    def validate(self, params: dict[str, Any]) -> None:
        self._resolve(params)

    def push(self, artifact_path: Path, params: dict[str, Any]) -> PushResult:
        dest = self._resolve(params)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact_path, dest)
        # Verify the copy landed intact — cheap, and catches a
        # half-written copy on a full disk / interrupted write.
        src_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        dst_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        if src_hash != dst_hash:
            raise OSError(f"local_path copy verification failed for {dest}")
        return PushResult(status="success", detail=str(dest))


class RcloneRemoteDestination:
    """STUB — see module docstring. Validates config shape only."""

    def validate(self, params: dict[str, Any]) -> None:
        remote = params.get("remote")
        path = params.get("path")
        if not remote or not isinstance(remote, str):
            raise DestinationConfigError(
                "rclone_remote destination requires a non-empty 'remote' "
                "string (the rclone remote NAME — never a credential value)"
            )
        if not path or not isinstance(path, str):
            raise DestinationConfigError(
                "rclone_remote destination requires a non-empty 'path' string"
            )

    def push(self, artifact_path: Path, params: dict[str, Any]) -> PushResult:
        raise RemotePushNotImplementedError(
            "rclone_remote push is a stubbed extension point (Wave E did "
            "not implement it) — see services/backup_destinations.py "
            "module docstring. The config is saved and validated; the "
            "encrypted artifact stays available for manual download via "
            "GET /api/v1/scheduled-backups/runs/{run_id}/download."
        )


def get_destination(destination_type: str) -> Destination:
    if destination_type == "local_path":
        return LocalPathDestination()
    if destination_type == "rclone_remote":
        return RcloneRemoteDestination()
    raise DestinationConfigError(f"Unknown destination_type: {destination_type!r}")
