"""runs: the run queue

One row per request to start or resume a thread. Workers lease rows with
SELECT ... FOR UPDATE SKIP LOCKED (jhunt/platform/runtime/queue.py, ADR 0005).
Rows change status only; they are never deleted.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE runs (
            id          bigserial PRIMARY KEY,
            thread_id   text NOT NULL,
            workflow    text NOT NULL,
            version     integer NOT NULL,
            -- start: begin the thread with payload as input.
            -- resume: continue a thread stopped at an interrupt, payload is the resume value.
            kind        text NOT NULL CHECK (kind IN ('start', 'resume')),
            payload     jsonb,
            status      text NOT NULL DEFAULT 'queued' CHECK (status IN
                            ('queued', 'running', 'interrupted', 'succeeded', 'failed')),
            -- A fresh uuid per claim: the fencing token. Every later write by a
            -- worker is conditional on it, so a worker that lost its lease can't
            -- extend or close the run.
            lease_owner uuid,
            lease_until timestamptz,
            attempts    integer NOT NULL DEFAULT 0,
            error       text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One active run per thread (AGENTS.md hard rule 11). The claim query
    # already never picks a second run on a busy thread; this index makes the
    # database refuse it too, should the query ever be wrong.
    op.execute(
        "CREATE UNIQUE INDEX runs_one_running_per_thread"
        " ON runs (thread_id) WHERE status = 'running'"
    )
    # The claim query filters by status and orders by id.
    op.execute("CREATE INDEX runs_status_id ON runs (status, id)")
    # It also finds the oldest open run of each thread.
    op.execute("CREATE INDEX runs_thread_status ON runs (thread_id, status)")


def downgrade() -> None:
    op.execute("DROP TABLE runs")  # its indexes go with it
