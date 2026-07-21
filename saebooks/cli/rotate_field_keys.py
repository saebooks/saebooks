"""``rotate-field-keys`` — re-encrypt every Fernet field under the primary key.

The operational half of key rotation (design: ``docs/security/key-management-plan.md``
§2). Run AFTER prepending the new key to ``SAEBOOKS_FIELD_ENCRYPTION_KEY``
(``new,old``) and BEFORE dropping the old key from the list::

    python -m saebooks.cli rotate-field-keys            # rotate everything
    python -m saebooks.cli rotate-field-keys --dry-run  # count, touch nothing

Properties:

* **No plaintext exposure** — uses ``MultiFernet.rotate`` via
  ``crypto.rotate_token``; secrets are never decrypted into CLI memory as
  application-visible values.
* **Idempotent + resumable** — batched by primary key; a re-run re-rotates
  (harmless) and an interrupted run just leaves some rows on the old key,
  which the next run picks up. Nothing depends on completing in one pass.
* **Fail-loud** — a token no configured key can decrypt aborts the run with
  the table/row identified. That row's secret would otherwise be silently
  lost at old-key retirement; surfacing it is the whole point.
* **Maintenance role** — rotation is a whole-database maintenance operation;
  run it as the owner role (the RLS ``SET LOCAL`` tenant dance does not apply
  to a walk that must touch every tenant's ciphertext).

The registry below is the canonical inventory of encrypted columns. Adding a
new ``*_encrypted`` column to a model means adding it here — the coverage
test (``tests/services/test_crypto_rotation.py``) greps the models and fails
if the registry falls behind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from saebooks.db import AsyncSessionLocal
from saebooks.services import crypto

logger = logging.getLogger(__name__)

BATCH = 200


@dataclass(frozen=True)
class EncryptedColumns:
    table: str
    pk: str
    columns: tuple[str, ...]
    binary: bool = False  # LargeBinary columns store the ASCII token as bytes


# Canonical inventory of Fernet-encrypted storage (see module docstring).
REGISTRY: tuple[EncryptedColumns, ...] = (
    EncryptedColumns(
        "employees",
        "id",
        ("tfn_encrypted", "isikukood_encrypted", "bsb_encrypted",
         "account_number_encrypted", "account_name_encrypted"),
    ),
    EncryptedColumns(
        "super_funds",
        "id",
        ("smsf_bsb_encrypted", "smsf_account_number_encrypted", "smsf_account_name_encrypted"),
    ),
    EncryptedColumns(
        "ato_sbr_configs", "id", ("keystore_encrypted", "keystore_password_encrypted")
    ),
    EncryptedColumns(
        "companies", "id", ("siss_client_secret_encrypted", "siss_subscription_key_encrypted")
    ),
    EncryptedColumns("paperless_webhook_secrets", "id", ("secret_ciphertext",), binary=True),
    EncryptedColumns("ic_edges", "id", ("relay_privkey_ciphertext",), binary=True),
    EncryptedColumns(
        "sync_connections",
        "id",
        ("oauth_client_id_ciphertext", "oauth_client_secret_ciphertext",
         "oauth_refresh_token_ciphertext"),
        binary=True,
    ),
)


def _as_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = bytes(value)
    return value.decode("ascii") if isinstance(value, bytes) else value


async def rotate_field_keys(*, dry_run: bool = False) -> int:
    """Walk the registry; re-encrypt every non-empty token under the primary key."""
    fingerprints = crypto.key_fingerprints()
    if not fingerprints:
        logger.error("rotate-field-keys: SAEBOOKS_FIELD_ENCRYPTION_KEY is not set")
        return 1
    logger.info(
        "rotate-field-keys: %d key(s) configured, primary=%s%s",
        len(fingerprints),
        fingerprints[0],
        " (dry run)" if dry_run else "",
    )

    total_rotated = 0
    async with AsyncSessionLocal() as session:
        for spec in REGISTRY:
            rotated = 0
            cursor = None
            while True:
                where = f"WHERE {spec.pk} > :cursor" if cursor is not None else ""
                rows = (
                    await session.execute(
                        text(
                            f"SELECT {spec.pk}, {', '.join(spec.columns)} "
                            f"FROM {spec.table} {where} "
                            f"ORDER BY {spec.pk} LIMIT {BATCH}"
                        ),
                        {"cursor": cursor} if cursor is not None else {},
                    )
                ).all()
                if not rows:
                    break
                for row in rows:
                    pk_value = row[0]
                    cursor = pk_value
                    updates: dict[str, str | bytes] = {}
                    for name, stored in zip(spec.columns, row[1:], strict=True):
                        token = _as_text(stored)
                        if not token:
                            continue
                        try:
                            new_token = crypto.rotate_token(token)
                        except crypto.FieldDecryptionError:
                            logger.error(
                                "rotate-field-keys: %s.%s (%s=%s) does not decrypt "
                                "with ANY configured key — aborting so the secret "
                                "is not lost at old-key retirement",
                                spec.table,
                                name,
                                spec.pk,
                                pk_value,
                            )
                            return 1
                        if new_token != token:
                            updates[name] = new_token.encode("ascii") if spec.binary else new_token
                    if updates and not dry_run:
                        sets = ", ".join(f"{c} = :{c}" for c in updates)
                        await session.execute(
                            text(
                                f"UPDATE {spec.table} SET {sets} WHERE {spec.pk} = :pk"
                            ),
                            {**updates, "pk": pk_value},
                        )
                    if updates:
                        rotated += 1
                if not dry_run:
                    await session.commit()
            total_rotated += rotated
            logger.info(
                "rotate-field-keys: %s — %d row(s) %s",
                spec.table,
                rotated,
                "would rotate" if dry_run else "rotated",
            )
        if not dry_run:
            await session.commit()

    logger.info(
        "rotate-field-keys: done — %d row(s) %s under primary %s",
        total_rotated,
        "would rotate" if dry_run else "now",
        fingerprints[0],
    )
    return 0
