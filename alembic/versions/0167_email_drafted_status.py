"""Allow 'drafted' in email_send_log.resend_status.

Draft mode (SAEBOOKS_EMAIL_DRAFT_MODE) parks composed customer emails in
the operator's Outlook drafts folder via Microsoft Graph instead of
sending; the audit row records resend_status='drafted' with the Graph
draft id reused in resend_message_id. This migration widens the CHECK
constraint introduced in 0123 to admit the new status.

Revision ID: 0167_email_drafted_status
Revises: 0166_tax_code_jurisdiction
"""
from __future__ import annotations

from alembic import op

revision: str = "0167_email_drafted_status"
down_revision: str | None = "0166_tax_code_jurisdiction"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_email_send_log_status_valid"
_TABLE = "email_send_log"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "resend_status IN ('sent','failed','blocked','queued','drafted')",
    )


def downgrade() -> None:
    # Any 'drafted' rows must be re-labelled before the narrower CHECK can
    # be restored; 'blocked' is the closest no-send-occurred status.
    op.execute(
        "UPDATE email_send_log SET resend_status = 'blocked' "
        "WHERE resend_status = 'drafted'"
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "resend_status IN ('sent','failed','blocked','queued')",
    )
