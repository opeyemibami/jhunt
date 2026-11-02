"""facts.aliases: the names a skill goes by (ADR 0015)

The claim validator checks a skill's level wherever a claim mentions it, cited
or not (owner, PR 3.6a review). A skill fact may list the names it goes by
(aliases = ["Kubernetes", "k8s"]); only skills have them. Existing rows get an
empty list: ADD COLUMN with a default fires no row trigger, so the append-only
rule (migration 0010) holds.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE facts ADD COLUMN aliases text[] NOT NULL DEFAULT '{}'")
    op.execute(
        "ALTER TABLE facts ADD CONSTRAINT facts_aliases_skills_only"
        " CHECK (aliases = '{}' OR kind = 'skill')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE facts DROP COLUMN aliases")
