"""CDR / GDPR-style data export helper.

Before a company revokes its SISS client (Batch K offboarding) we dump
every locally-held bank-feed artefact to disk so the user has a
verifiable copy. This is a legal requirement under CDR and a good
practice regardless — revoking upstream doesn't destroy local data,
but giving the user a ready-to-use bundle is part of the offboard UX.

Output: one JSON file per company, ``bank-feed-export-<company_id>-<ts>.json``,
containing:

    {
      "exported_at": "...",
      "company_id": "...",
      "bank_feed_client": {...},
      "accounts": [
        {
          "account": {...},
          "statement_lines": [...]
        },
        ...
      ]
    }

No SISS call is made here — purely local. Safe to run even when
credentials are missing.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.bank_statement import BankStatementLine


async def write_cdr_export(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    export_dir: str,
) -> str:
    """Serialise all bank-feed data for ``company_id``; return the file path."""
    os.makedirs(export_dir, exist_ok=True)

    client = (
        await session.execute(
            select(BankFeedClient).where(BankFeedClient.company_id == company_id)
        )
    ).scalar_one_or_none()

    feed_accounts = (
        await session.execute(
            select(BankFeedAccount).where(BankFeedAccount.company_id == company_id)
        )
    ).scalars().all()

    payload: dict[str, object] = {
        "exported_at": datetime.now().isoformat(),
        "company_id": str(company_id),
        "bank_feed_client": _client_dict(client) if client is not None else None,
        "accounts": [],
    }

    for feed in feed_accounts:
        lines = (
            await session.execute(
                select(BankStatementLine).where(
                    BankStatementLine.bank_feed_account_id == feed.id
                )
                .order_by(BankStatementLine.txn_date)
            )
        ).scalars().all()
        assert isinstance(payload["accounts"], list)
        payload["accounts"].append(
            {
                "account": _feed_account_dict(feed),
                "statement_lines": [_line_dict(ln) for ln in lines],
            }
        )

    filename = f"bank-feed-export-{company_id}-{int(datetime.now().timestamp())}.json"
    path = os.path.join(export_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str, sort_keys=True)
    return path


def _client_dict(c: BankFeedClient) -> dict[str, object]:
    return {
        "id": str(c.id),
        "sds_client_id": c.sds_client_id,
        "active": c.active,
        "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _feed_account_dict(a: BankFeedAccount) -> dict[str, object]:
    return {
        "id": str(a.id),
        "sds_account_id": a.sds_account_id,
        "sds_institution_id": a.sds_institution_id,
        "masked_number": a.masked_number,
        "display_name": a.display_name,
        "product_category": a.product_category,
        "feed_type": a.feed_type,
        "processing_status": a.processing_status,
        "processing_status_date": (
            a.processing_status_date.isoformat() if a.processing_status_date else None
        ),
        "last_transaction_posted_id": a.last_transaction_posted_id,
        "last_transaction_posted_date": (
            a.last_transaction_posted_date.isoformat()
            if a.last_transaction_posted_date
            else None
        ),
        "ledger_account_id": str(a.ledger_account_id),
        "revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
    }


def _line_dict(ln: BankStatementLine) -> dict[str, object]:
    return {
        "id": str(ln.id),
        "txn_date": ln.txn_date.isoformat() if ln.txn_date else None,
        "description": ln.description,
        "amount": str(ln.amount),
        "reference": ln.reference,
        "status": str(ln.status),
        "external_id": ln.external_id,
        "matched_entry_id": (
            str(ln.matched_entry_id) if ln.matched_entry_id else None
        ),
    }
