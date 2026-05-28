"""0136_api_tokens_rls — enable + force RLS on api_tokens.

Round-3 RLS audit (2026-05-25) found api_tokens shipped in mig 0110 with
tenant_id + FK but no RLS policy and no ENABLE/FORCE — i.e. token metadata
(name, prefix, scopes, last_used_at) leaks cross-tenant on any code path
that hits the table without an explicit tenant_id filter.

This migration closes the gap using the canonical policy form (see 0041,
0055, 0083, 0118): USING/WITH CHECK on tenant_id =
current_setting(app.current_tenant, true)::uuid.

Revision ID: 0136_api_tokens_rls
Revises: 0135_user_webauthn_credentials
Create Date: 2026-05-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0136_api_tokens_rls"
down_revision: str | None = "0135_user_webauthn_credentials"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"


def upgrade() -> None:
    op.execute("ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_tokens FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON api_tokens "
        f"USING {_USING} WITH CHECK {_USING}"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_tokens")
    op.execute("ALTER TABLE api_tokens NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_tokens DISABLE ROW LEVEL SECURITY")
