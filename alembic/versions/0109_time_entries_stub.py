"""0109_time_entries — stub bridging 0104 → 0109 on this branch.

This file exists to repair a divergent-branch situation on r420: the
production DB was stamped at ``0109_time_entries`` by an earlier
deploy of the ``feat/cashbook-persistence`` branch, but ``main``
never had the matching migration file. Every subsequent deploy from
``main`` crashes with ``Can't locate revision identified by
'0109_time_entries'``.

To unblock our deploys without rolling the DB back (and losing the
time_entries schema that's already there), this stub registers the
revision id with a no-op upgrade. When ``feat/cashbook-persistence``
eventually lands, that branch's *real* 0109 file will need to be
reconciled — either this stub deleted in the same merge commit, or
the real file renamed. Tracked in ``DEFERRED.md``.

The down_revision points at ``0104_journal_lines_tax_treatment``
(``main``'s actual head before this branch). Anything that
``feat/cashbook-persistence`` did between 0104 and 0109 (0105 / 0106
/ 0107 / 0108) is already present in the DB; the stub assumes
forward-only.
"""
from __future__ import annotations

revision = "0109_time_entries"
down_revision = "0104_journal_lines_tax_treatment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op — the schema this revision represents is already in the
    # DB on r420 from a prior deploy. On a fresh DB (CI, new
    # self-host installs), the time_entries tables simply won't
    # exist; that's fine for this branch because no code on this
    # branch touches them.
    pass


def downgrade() -> None:
    # No-op — we don't model the inverse. Downgrading past this
    # revision on a DB that DOES have the time_entries tables would
    # leave them orphaned; a real ``feat/cashbook-persistence`` merge
    # will replace this stub with the proper downgrade.
    pass
