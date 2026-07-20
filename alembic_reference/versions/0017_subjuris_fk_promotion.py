"""Sub-jurisdiction FK promotion (M1.5 · 5-SUBJURIS, K5 breadth).

Purely additive, following the T3/T4 pattern (nullable FK columns +
backfill + AU seed proving parity; no posting-path change):

* ``jurisdictions.code`` / ``jurisdictions.parent_code`` widen
  ``String(3)`` → ``String(6)`` so sub-national nodes can use their ISO
  3166-2 code as the primary key (``AU-QLD``, ``US-CA``, ``GB-ENG``).
  The earlier "AUQ"-style 3-char convention was never seeded and
  collides with the ISO 3166-1 alpha-3 country space ("AUS" = Australia
  blocks South Australia; "AUT" = Austria blocks Tasmania). Widening a
  varchar is metadata-only in Postgres; every existing 3-char value and
  every existing ``String(3)`` FK column (which only ever hold
  country-level codes) is untouched.

* The four ad-hoc ``state: String`` columns flagged by the K5 audit —
  ``holiday_calendars``, ``bank_routing_directory``,
  ``payroll_tax_rates``, ``duty_rate_schedules`` — each gain a NULLABLE
  ``sub_jurisdiction_code`` FK into the T3 tree. The old ``state``
  string columns are KEPT (additive transition; nothing consuming them
  changes behaviour).

* The eight AU state/territory nodes are inserted idempotently — but
  ONLY when the ``AUS`` country row already exists (a fresh DB runs
  migrations before the seed loader, so the parent may be absent; the
  ``AU/jurisdictions.yaml`` seed added in this slice owns the rows from
  then on and the loader upsert converges both paths).

* Backfill: AU rows in the four tables resolve their ``state`` string
  against the tree generically — ``jurisdictions.parent_code`` = the
  row's country and the ISO 3166-2 suffix = the ``state`` value
  ('QLD' → 'AU-QLD'). ``bank_routing_directory`` has no ``jurisdiction``
  column (BSB is AU-only) so its rows resolve under the 'AUS' parent
  explicitly. Rows whose state has no matching node stay NULL — never
  a hard failure.

Reversible: downgrade drops the four columns, deletes sub-national
jurisdiction rows (codes longer than 3 chars — only this migration and
its seed create them), and narrows the code columns back.

Revision ID: 0016_subjuris_fk_promotion
Revises: 0015_duty_domain_gaps
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_subjuris_fk_promotion"
down_revision: str | None = "0015_duty_domain_gaps"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# The four tables whose ad-hoc ``state`` string gains an FK sibling.
# Table name → does it carry a country-level ``jurisdiction`` column?
_TABLES = {
    "holiday_calendars": True,
    "bank_routing_directory": False,  # BSB directory — AU-implicit
    "payroll_tax_rates": True,
    "duty_rate_schedules": True,
}

# The eight AU state/territory nodes (ISO 3166-2:AU), inserted only when
# the AUS parent row exists. The AU/jurisdictions.yaml seed owns these
# rows once the loader runs; ON CONFLICT keeps the two paths convergent.
_AU_SUBDIVISIONS = (
    ("AU-NSW", "New South Wales"),
    ("AU-VIC", "Victoria"),
    ("AU-QLD", "Queensland"),
    ("AU-SA", "South Australia"),
    ("AU-WA", "Western Australia"),
    ("AU-TAS", "Tasmania"),
    ("AU-NT", "Northern Territory"),
    ("AU-ACT", "Australian Capital Territory"),
)

# Generic state-string → tree resolution: the row's country owns a node
# whose ISO 3166-2 suffix equals the state string ('QLD' matches
# 'AU-QLD' under parent 'AUS'; 'ENG' would match 'GB-ENG' under 'GBR').
_BACKFILL_WITH_JURISDICTION = """
    UPDATE {table} AS t
    SET sub_jurisdiction_code = j.code
    FROM jurisdictions AS j
    WHERE t.sub_jurisdiction_code IS NULL
      AND t.state IS NOT NULL
      AND j.parent_code = t.jurisdiction
      AND split_part(j.iso_subdivision_code, '-', 2) = t.state
"""

_BACKFILL_AU_IMPLICIT = """
    UPDATE {table} AS t
    SET sub_jurisdiction_code = j.code
    FROM jurisdictions AS j
    WHERE t.sub_jurisdiction_code IS NULL
      AND t.state IS NOT NULL
      AND j.parent_code = 'AUS'
      AND split_part(j.iso_subdivision_code, '-', 2) = t.state
"""


def upgrade() -> None:
    # 1. Widen the tree's key columns (metadata-only for Postgres).
    op.alter_column(
        "jurisdictions",
        "code",
        type_=sa.String(6),
        existing_type=sa.String(3),
        existing_nullable=False,
    )
    op.alter_column(
        "jurisdictions",
        "parent_code",
        type_=sa.String(6),
        existing_type=sa.String(3),
        existing_nullable=True,
    )

    # 2. The nullable FK columns (state strings kept — additive).
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "sub_jurisdiction_code",
                sa.String(6),
                sa.ForeignKey(
                    "jurisdictions.code",
                    name=f"fk_{table}_sub_jurisdiction_code",
                ),
                nullable=True,
            ),
        )

    # 3. AU state/territory nodes — only when the AUS parent exists
    #    (live DBs; fresh DBs get them from AU/jurisdictions.yaml).
    for code, name in _AU_SUBDIVISIONS:
        op.execute(
            sa.text(
                "INSERT INTO jurisdictions "
                "(code, name, currency_default, decimal_places, active, "
                " parent_code, level, iso_subdivision_code) "
                "SELECT :code, :name, 'AUD', 2, true, 'AUS', 'state', :code "
                "WHERE EXISTS (SELECT 1 FROM jurisdictions WHERE code = 'AUS') "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, name=name)
        )

    # 4. Backfill AU (and any other resolvable) state strings.
    for table, has_jurisdiction in _TABLES.items():
        tmpl = (
            _BACKFILL_WITH_JURISDICTION
            if has_jurisdiction
            else _BACKFILL_AU_IMPLICIT
        )
        op.execute(sa.text(tmpl.format(table=table)))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(
            f"fk_{table}_sub_jurisdiction_code", table, type_="foreignkey"
        )
        op.drop_column(table, "sub_jurisdiction_code")
    # Sub-national nodes only exist via this migration / its seed; remove
    # them so the key columns can narrow back to String(3).
    op.execute(sa.text("DELETE FROM jurisdictions WHERE length(code) > 3"))
    op.alter_column(
        "jurisdictions",
        "parent_code",
        type_=sa.String(3),
        existing_type=sa.String(6),
        existing_nullable=True,
    )
    op.alter_column(
        "jurisdictions",
        "code",
        type_=sa.String(3),
        existing_type=sa.String(6),
        existing_nullable=False,
    )
