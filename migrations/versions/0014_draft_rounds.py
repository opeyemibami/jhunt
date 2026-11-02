"""draft_claims.round: the first draft (0) and its one repair round (1)

When a draft has blocked claims, the whole draft goes back to the model once,
and its replacements are checked like any other claim (owner, after the
first real pack; ADR 0016). Both rounds are kept: round 0 as drafted, its
blocked claims never reworded, and round 1, the one used. Adding the column
and widening the key fire no row trigger, so draft_claims stays append-only.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE draft_claims ADD COLUMN round smallint NOT NULL DEFAULT 0"
        " CHECK (round IN (0, 1))"
    )
    op.execute("ALTER TABLE draft_claims DROP CONSTRAINT draft_claims_pkey")
    op.execute("ALTER TABLE draft_claims ADD PRIMARY KEY (draft_id, round, section, position)")


def downgrade() -> None:
    # Round 1 rows would collide with round 0's key; the column can only go
    # if there are none. A repaired draft can't be downgraded in place.
    op.execute("ALTER TABLE draft_claims DROP CONSTRAINT draft_claims_pkey")
    op.execute("ALTER TABLE draft_claims ADD PRIMARY KEY (draft_id, section, position)")
    op.execute("ALTER TABLE draft_claims DROP COLUMN round")
