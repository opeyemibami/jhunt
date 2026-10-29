"""langgraph checkpointer

Creates the LangGraph Postgres checkpointer's tables by calling
PostgresSaver.setup(), as of langgraph-checkpoint-postgres 3.2.0. setup() is
never called at app startup. When that package is upgraded, add a new migration
that calls setup() again; tests/test_migrations.py fails until you do.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 20:25:49.005396
"""

from collections.abc import Sequence

from alembic import op
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table setup() creates in 3.2.0; their indexes go with them.
CHECKPOINTER_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


def upgrade() -> None:
    # setup() runs CREATE INDEX CONCURRENTLY, which Postgres refuses inside a
    # transaction. autocommit_block() commits Alembic's transaction first, so
    # migrations before this one in the same run are already committed.
    with op.get_context().autocommit_block():
        url = op.get_bind().engine.url.set(drivername="postgresql")
        # A separate connection, set up the way PostgresSaver expects
        # (AGENTS.md: autocommit=True, row_factory=dict_row).
        with Connection.connect(
            url.render_as_string(hide_password=False), autocommit=True, row_factory=dict_row
        ) as conn:
            PostgresSaver(conn).setup()


def downgrade() -> None:
    for table in CHECKPOINTER_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
