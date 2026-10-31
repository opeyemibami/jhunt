"""gateway v0: tool_calls (the audit log) and autonomy_settings

tool_calls holds one row per completed tool call, keyed by its idempotency
key, with the recorded result a repeated call returns instead of acting again.
autonomy_settings holds each action's level; the latest row per action is the
current level, and history is kept (AGENTS.md hard rule 10). Both are
append-only (hard rule 8), through forbid_change() from migration 0005. See
ADR 0008.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Level 1: act internally and reversibly, then notify (AGENTS.md, autonomy
# policy). Week 2's Capture needs these two.
SEEDED = [("fetch_postings", 1), ("snapshot_job_description", 1)]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE autonomy_settings (
            id         bigserial PRIMARY KEY,
            action     text NOT NULL,
            level      integer NOT NULL CHECK (level IN (0, 1, 2)),
            changed_at timestamptz NOT NULL DEFAULT now(),
            changed_by text NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX autonomy_settings_action ON autonomy_settings (action, id)")
    for action, level in SEEDED:
        op.execute(
            "INSERT INTO autonomy_settings (action, level, changed_by)"
            f" VALUES ('{action}', {level}, 'migration 0006')"
        )
    _append_only("autonomy_settings")

    op.execute(
        """
        CREATE TABLE tool_calls (
            id              bigserial PRIMARY KEY,
            -- hash of [thread_id, node, step_key] (hard rule 3); one row per key
            idempotency_key text NOT NULL UNIQUE,
            thread_id       text NOT NULL,
            node            text NOT NULL,
            step_key        text NOT NULL,
            run_id          bigint,
            tool            text NOT NULL,
            args_hash       text NOT NULL,
            level           integer NOT NULL,
            approved_by     text,
            outcome         text NOT NULL,
            -- what a repeated call returns: small references, never page content
            result          jsonb NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    _append_only("tool_calls")


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE tool_calls")
    op.execute("DROP TABLE autonomy_settings")
