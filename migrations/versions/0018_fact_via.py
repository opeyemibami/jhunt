"""facts.via: whether a version came from a command or the Facts page (ADR 0017)

The database is the facts' source of truth (owner, UI v1). An import from the
owner's file must not silently undo an edit made on the Facts page since the
file was exported, so each version records where it was made: "command"
(jhunt-facts import or retire, as every version so far) or "browser".

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE facts ADD COLUMN via text NOT NULL DEFAULT 'command'"
        " CHECK (via IN ('command', 'browser'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE facts DROP COLUMN via")
