"""postings.fit_matches: what a posting's fit score matched (ADR 0011)

The fit score is stored with what matched it ({"title": [...], "include":
[...], "penalty": [...], "exclude": [...]}), so the digest can show why a
posting scored what it did. Null until the posting is scored.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE postings ADD COLUMN fit_matches jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE postings DROP COLUMN fit_matches")
