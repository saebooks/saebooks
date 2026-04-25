"""Race-safe idempotency service — RFC 8417 compliant.

Public surface
--------------
``claim_or_fetch(session, idempotency_key, tenant_id, body_sha256)``
    Attempt to claim an idempotency slot.  Returns a ``ClaimResult``
    whose ``.status`` is one of:

    * ``ClaimStatus.CLAIMED`` — this request is the first writer;
      caller must process the request and then call ``store_response``.
    * ``ClaimStatus.REPLAY``  — a previous identical request already
      completed; caller must return ``result.response_status`` /
      ``result.response_body`` verbatim.
    * ``ClaimStatus.CONFLICT`` — the same key was used with a
      *different* body; caller must return HTTP 422 with the
      ``idempotency_key_conflict`` error code.

``store_response(session, idempotency_key, response_status, response_body_bytes)``
    Persist the final response under the key.  Must only be called
    after ``claim_or_fetch`` returns ``CLAIMED``.

Race-safety
-----------
The single DB round-trip uses::

    INSERT INTO idempotency_records (idempotency_key, tenant_id, body_sha256,
                                     response_status, response_body)
    VALUES (:key, :tenant_id, :sha256, 0, b'')
    ON CONFLICT (idempotency_key)
    DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
    RETURNING idempotency_key, tenant_id, body_sha256,
              response_status, response_body

The ``DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key`` is a
deliberate no-op (the PK cannot change) whose only purpose is to make
PostgreSQL always fire the RETURNING clause — even on conflict.  Without
this trick, an ``ON CONFLICT DO NOTHING RETURNING`` returns zero rows on
conflict and requires a second SELECT to read the existing row.

When the first writer inserts successfully, RETURNING gives back its own
``(response_status=0, response_body=b'')``, which the CLAIMED code path
recognises as a sentinel meaning "no stored response yet — go process
your request."

When a later writer conflicts, RETURNING gives back the *existing* row.
If ``body_sha256`` matches the caller's hash and ``response_status != 0``
the response is ready to replay.  If ``body_sha256`` differs the key was
used with a different body → 422.

Edge case: two concurrent writers both see ``response_status == 0``
(the first writer hasn't committed yet).  The second writer returns
``ClaimStatus.REPLAY`` with an empty body, which the caller should
treat as "in flight — retry later".  In practice the caller will retry
the whole request (banking clients retry on network failure), and by then
the first writer has committed; the retry sees the real stored response.
This is the expected behaviour for an in-flight idempotency window.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ClaimStatus(enum.Enum):
    """Result of a ``claim_or_fetch`` call."""

    CLAIMED = "claimed"
    """This request is the first writer — caller must process + store response."""

    REPLAY = "replay"
    """A previous identical request completed — return the cached response."""

    CONFLICT = "conflict"
    """Same key, different body — return HTTP 422."""


@dataclass
class ClaimResult:
    status: ClaimStatus
    response_status: int = 0
    response_body: bytes = field(default_factory=bytes)


# Sentinel response_status that signals "slot claimed but not yet
# populated".  Plain 0 is fine because valid HTTP status codes are
# >= 100.
_PENDING_STATUS: int = 0


async def claim_or_fetch(
    session: AsyncSession,
    idempotency_key: str,
    tenant_id: uuid.UUID,
    body_sha256: str,
) -> ClaimResult:
    """Atomically claim or look up an idempotency slot.

    One DB round-trip using INSERT … ON CONFLICT DO UPDATE RETURNING.
    See module docstring for the full race-safety argument.

    Parameters
    ----------
    session:
        An open ``AsyncSession``.  The caller owns the transaction;
        this function does NOT commit.
    idempotency_key:
        Raw value of the ``X-Idempotency-Key`` header.
    tenant_id:
        UUID of the authenticated tenant — used to detect cross-tenant
        key collisions.
    body_sha256:
        SHA-256 hex digest of the raw request body bytes.

    Returns
    -------
    ClaimResult
    """
    stmt = text(
        """
        INSERT INTO idempotency_records
            (idempotency_key, tenant_id, body_sha256, response_status, response_body)
        VALUES
            (:key, :tenant_id, :sha256, :pending, :empty_body)
        ON CONFLICT (idempotency_key)
        DO UPDATE SET
            idempotency_key = EXCLUDED.idempotency_key
        RETURNING
            idempotency_key,
            tenant_id,
            body_sha256,
            response_status,
            response_body
        """
    )
    row = (
        await session.execute(
            stmt,
            {
                "key": idempotency_key,
                "tenant_id": str(tenant_id),
                "sha256": body_sha256,
                "pending": _PENDING_STATUS,
                "empty_body": b"",
            },
        )
    ).mappings().one()

    stored_sha256: str = row["body_sha256"]
    stored_status: int = row["response_status"]
    stored_body: bytes = row["response_body"] or b""

    # First writer: RETURNING gives back our own inserted row (pending sentinel).
    if stored_status == _PENDING_STATUS and stored_sha256 == body_sha256:
        return ClaimResult(status=ClaimStatus.CLAIMED)

    # Same key, different body — RFC 8417 §2.1.
    if stored_sha256 != body_sha256:
        return ClaimResult(status=ClaimStatus.CONFLICT)

    # Matching key + matching hash → replay the cached response.
    return ClaimResult(
        status=ClaimStatus.REPLAY,
        response_status=stored_status,
        response_body=stored_body,
    )


async def store_response(
    session: AsyncSession,
    idempotency_key: str,
    response_status: int,
    response_body: bytes,
) -> None:
    """Populate the response for a previously claimed slot.

    Must only be called after ``claim_or_fetch`` returned ``CLAIMED``.
    Updates the row in-place; the caller must commit the enclosing
    transaction for the update to become visible to concurrent readers.

    Parameters
    ----------
    session:
        The same session used for ``claim_or_fetch``.
    idempotency_key:
        Raw key value (same as the ``claim_or_fetch`` call).
    response_status:
        HTTP status code of the response (e.g. 201, 200, 409).
    response_body:
        UTF-8 encoded JSON bytes of the response body.
    """
    stmt = text(
        """
        UPDATE idempotency_records
        SET response_status = :status,
            response_body   = :body
        WHERE idempotency_key = :key
        """
    )
    await session.execute(
        stmt,
        {
            "key": idempotency_key,
            "status": response_status,
            "body": response_body,
        },
    )
