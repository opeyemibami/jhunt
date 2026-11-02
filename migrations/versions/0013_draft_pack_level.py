"""autonomy action draft_application_pack at level 0 (ADR 0016)

"Draft CV tailoring, cover note, answers: 0" (AGENTS.md, autonomy policy).
The draft_pack tool runs under it. Level 0 is suggest only: the gateway runs
it because the tool only stores a draft for the owner (Tool.suggests), and
would refuse any tool that acts.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO autonomy_settings (action, level, changed_by)"
        " VALUES ('draft_application_pack', 0, 'migration 0013')"
    )


def downgrade() -> None:
    # autonomy_settings is append-only; the migration's own row goes with it.
    op.execute("ALTER TABLE autonomy_settings DISABLE TRIGGER autonomy_settings_append_only")
    op.execute(
        "DELETE FROM autonomy_settings"
        " WHERE action = 'draft_application_pack' AND changed_by = 'migration 0013'"
    )
    op.execute("ALTER TABLE autonomy_settings ENABLE TRIGGER autonomy_settings_append_only")
