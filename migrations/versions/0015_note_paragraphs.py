"""draft_claims.paragraph: each cover-note sentence's group (ADR 0016)

The model tags each cover-note sentence with a group, experience, projects or
community, and the note has one paragraph per group, in that order (owner,
after the first real pack). CV lines have none. Drafts from before this keep
NULL and render as one paragraph. ADD COLUMN fires no row trigger, so
draft_claims stays append-only.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE draft_claims ADD COLUMN paragraph text"
        " CHECK (paragraph IN ('experience', 'projects', 'community'))"
    )
    op.execute(
        "ALTER TABLE draft_claims ADD CONSTRAINT draft_claims_paragraph_note_only"
        " CHECK (paragraph IS NULL OR section = 'cover_note')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE draft_claims DROP COLUMN paragraph")
