"""draft_edits: the owner's edits to drafted lines, one row per version (ADR 0017, U4)

An edit never changes the drafted claim (draft_claims stays as drafted, hard
rule 6): it's a new version of the line, created_by you, and the latest
version is what's shown and copied. The claim validator's problems with the
edited text are kept as warnings; they never block saving (owner, UI v1).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE draft_edits (
            id         bigserial PRIMARY KEY,
            draft_id   bigint NOT NULL,
            round      smallint NOT NULL,
            section    text NOT NULL,
            position   integer NOT NULL,
            version    integer NOT NULL CHECK (version >= 1),
            text       text NOT NULL CHECK (text <> ''),
            warnings   text[] NOT NULL,
            created_by text NOT NULL CHECK (created_by = 'you'),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (draft_id, round, section, position)
                REFERENCES draft_claims (draft_id, round, section, position),
            UNIQUE (draft_id, round, section, position, version)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER draft_edits_append_only BEFORE UPDATE OR DELETE ON draft_edits"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER draft_edits_no_truncate BEFORE TRUNCATE ON draft_edits"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE draft_edits")
