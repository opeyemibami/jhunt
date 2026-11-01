"""postings: every job Scout has seen on the boards in config/sources.toml

One row per job, keyed like Capture (board_job_key, ADR 0007), so a captured
posting and its application share a dedupe_key. Every posting is kept, in
scope or not: in_scope and scope_reason say why (ADR 0010). Rows are updated
when seen again, and removed 30 days after they were last seen unless
captured (design.md: "replaced daily"), so postings aren't append-only.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE postings (
            id            bigserial PRIMARY KEY,
            dedupe_key    text NOT NULL UNIQUE,
            provider      text NOT NULL,
            board         text NOT NULL,
            job_id        text NOT NULL,
            url           text NOT NULL,
            title         text NOT NULL,
            company       text NOT NULL,
            location      text NOT NULL,
            remote        boolean NOT NULL,
            -- The location filter's answer, and why ("Dublin", "region EMEA",
            -- "out: San Francisco"). Nothing is dropped.
            in_scope      boolean NOT NULL,
            scope_reason  text NOT NULL,
            -- Set by the fit score (PR 3.2); null until then.
            fit_score     real,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The digest: today's in-scope postings, best fit first.
    op.execute("CREATE INDEX postings_in_scope_seen ON postings (in_scope, last_seen_at)")


def downgrade() -> None:
    op.execute("DROP TABLE postings")
