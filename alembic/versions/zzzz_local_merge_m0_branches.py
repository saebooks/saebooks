"""Local-only merge node — joins out-of-band 0102_alloc_invariants
head into the M0 chain so the dev DB can upgrade. NOT TRACKED IN GIT."""
from collections.abc import Sequence

revision: str = "zzzz_local_merge_m0_branches"
down_revision = "0104_journal_lines_tax_treatment"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
