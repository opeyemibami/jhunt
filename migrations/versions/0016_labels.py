"""labels: the owner's judgements, append-only (design.md, Label)

design.md's Label: kind, target, at, "never changes; the eval data". Kinds
now: skip and unskip (a posting the owner isn't interested in, and its undo;
owner, UI v1). pick, draft_accepted and draft_edited are allowed for later.
A label is never changed or deleted (hard rule 8): an undo is a new label,
and the latest label for a target decides.

target is the posting's dedupe_key, which outlives the postings row (the
30-day cleanup), so a hidden posting stays hidden when Scout sees it again.
detail keeps what the owner saw (title, company, url) for the Hidden view.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE labels (
            id         bigserial PRIMARY KEY,
            kind       text NOT NULL CHECK (kind IN
                       ('skip', 'unskip', 'pick', 'draft_accepted', 'draft_edited')),
            target     text NOT NULL,
            detail     jsonb NOT NULL DEFAULT '{}',
            created_by text NOT NULL CHECK (created_by = 'you'),
            at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX labels_target ON labels (target, id)")
    op.execute(
        "CREATE TRIGGER labels_append_only BEFORE UPDATE OR DELETE ON labels"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER labels_no_truncate BEFORE TRUNCATE ON labels"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE labels")
