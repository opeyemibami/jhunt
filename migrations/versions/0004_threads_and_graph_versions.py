"""threads and graph_versions: version pinning

threads pins each thread to the workflow version it started on; a resume
always runs that version (AGENTS.md hard rule 12). graph_versions holds a
fingerprint of each registered version's nodes and edges, so a worker can
refuse a version that was changed in place (ADR 0006).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE threads (
            thread_id  text PRIMARY KEY,
            workflow   text NOT NULL,
            version    integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE graph_versions (
            workflow      text NOT NULL,
            version       integer NOT NULL,
            -- sha256 of the graph's node names and edges (ADR 0006)
            fingerprint   text NOT NULL,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (workflow, version)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE graph_versions")
    op.execute("DROP TABLE threads")
