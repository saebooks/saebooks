"""0195_merge_ee_permissions — join the EE statutory chain with granular permissions.

The feat/kmd-inf-tsd branch chained 0190_contact_registration_number →
0191/0192 (EE payroll compute) → 0193_employee_isikukood off
0189_scheduled_backups, while the planned-modules FINAL wave landed its
own chain ending at 0194_role_permissions_rls off the same parent in
parallel. After the branch merge alembic sees two heads.

No-op merge revision joining them so ``alembic upgrade head`` works
without --branchname. Both chains are purely additive; no ordering
dependency exists between them.

Revision ID: 0195_merge_ee_permissions
Revises: 0194_role_permissions_rls, 0193_employee_isikukood
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0195_merge_ee_permissions"
down_revision: tuple[str, str] = ("0194_role_permissions_rls", "0193_employee_isikukood")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
