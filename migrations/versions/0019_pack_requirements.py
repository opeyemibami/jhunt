"""pack_requirements, pack_requirement_checks; autonomy action extract_requirements (ADR 0019)

Application pack v2 lists what the job asks for and matches each item to the
owner's facts before drafting. Every requirement kept after the checks is a
row of pack_requirements; pack_requirement_checks has one row per draft
thread: how many the checks dropped, and which model answered. Both are
append-only, like draft_claims.

extract_requirements runs at level 0, suggest only: the tool only stores a
suggestion for the owner (Tool.suggests).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pack_requirement_checks (
            -- The draft's thread: the step runs before its pack_drafts row exists.
            thread_id  text PRIMARY KEY,
            dropped    integer NOT NULL CHECK (dropped >= 0),
            provider   text NOT NULL,
            model      text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE pack_requirements (
            thread_id text NOT NULL REFERENCES pack_requirement_checks (thread_id),
            position  integer NOT NULL CHECK (position >= 0),
            text      text NOT NULL CHECK (text <> ''),
            quote     text NOT NULL CHECK (quote <> ''),
            kind      text NOT NULL CHECK (kind IN ('must', 'nice')),
            match     text NOT NULL CHECK (match IN ('met', 'partial', 'gap')),
            fact_ids  text[] NOT NULL,
            why       text NOT NULL,
            PRIMARY KEY (thread_id, position),
            -- A gap cites nothing; met and partial cite at least one fact.
            CHECK ((match = 'gap') = (cardinality(fact_ids) = 0))
        )
        """
    )
    for table in ("pack_requirement_checks", "pack_requirements"):
        # Row-level for UPDATE and DELETE, statement-level for TRUNCATE (as in 0012).
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
        )
    op.execute(
        "INSERT INTO autonomy_settings (action, level, changed_by)"
        " VALUES ('extract_requirements', 0, 'migration 0019')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE pack_requirements")  # DROP fires no row or truncate triggers
    op.execute("DROP TABLE pack_requirement_checks")
    # autonomy_settings is append-only; the migration's own row goes with it.
    op.execute("ALTER TABLE autonomy_settings DISABLE TRIGGER autonomy_settings_append_only")
    op.execute(
        "DELETE FROM autonomy_settings"
        " WHERE action = 'extract_requirements' AND changed_by = 'migration 0019'"
    )
    op.execute("ALTER TABLE autonomy_settings ENABLE TRIGGER autonomy_settings_append_only")
