"""capture tables: jd_snapshots, applications, events

Once recorded, a snapshot or an event never changes (AGENTS.md hard rule 8):
triggers refuse UPDATE, DELETE and TRUNCATE on both, for every database role
the app uses. An application changes only its stage, outcome, notes and
closing time; its thread ids are set once (design.md, data model; ADR 0007).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One function for every append-only table; tool_calls, submissions and
    # labels get the same trigger when they are created.
    op.execute(
        """
        CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- restrict_violation (SQLSTATE 23001): the app sees an integrity error.
            RAISE EXCEPTION '% on % refused: rows are append-only (AGENTS.md hard rule 8)',
                TG_OP, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE jd_snapshots (
            id              bigserial PRIMARY KEY,
            url             text NOT NULL,
            -- The postings table arrives with Scout (week 3); a manual capture has none.
            posting_id      bigint,
            -- Each part is a record in the records bucket (ADR 0002): where it is,
            -- which exact generation, and the sha256 of its bytes.
            text_uri        text NOT NULL,
            text_generation bigint NOT NULL,
            text_sha256     text NOT NULL,
            html_uri        text NOT NULL,
            html_generation bigint NOT NULL,
            html_sha256     text NOT NULL,
            -- The PDF render comes later (docs/backlog.md).
            pdf_uri         text,
            captured_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    _append_only("jd_snapshots")

    op.execute(
        """
        CREATE TABLE applications (
            id             bigserial PRIMARY KEY,
            -- sha256 of the normalised URL, company and title. Unique: a posting
            -- is captured once (jhunt/workflows/capture/dedupe.py).
            dedupe_key     text NOT NULL UNIQUE,
            snapshot_id    bigint NOT NULL REFERENCES jd_snapshots (id),
            company        text NOT NULL,
            role           text NOT NULL,
            stage          text NOT NULL,
            opened_at      timestamptz NOT NULL DEFAULT now(),
            closed_at      timestamptz,
            outcome        text,
            notes          text,
            -- The lifecycle thread (week 3) and the chat thread (weeks 9 to 10).
            thread_id      text,
            chat_thread_id text
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION applications_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- What an application is never changes; only where it stands does.
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
               OR NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
               OR NEW.company IS DISTINCT FROM OLD.company
               OR NEW.role IS DISTINCT FROM OLD.role
               OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
                RAISE EXCEPTION 'application %: id, dedupe_key, snapshot_id, company, role'
                    ' and opened_at cannot change', OLD.id USING ERRCODE = 'restrict_violation';
            END IF;
            -- Thread ids are set once, from NULL, and then never move.
            IF (OLD.thread_id IS NOT NULL AND NEW.thread_id IS DISTINCT FROM OLD.thread_id)
               OR (OLD.chat_thread_id IS NOT NULL
                   AND NEW.chat_thread_id IS DISTINCT FROM OLD.chat_thread_id) THEN
                RAISE EXCEPTION 'application %: a thread id cannot change once set', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER applications_guard BEFORE UPDATE ON applications"
        " FOR EACH ROW EXECUTE FUNCTION applications_guard()"
    )

    op.execute(
        """
        CREATE TABLE events (
            id             bigserial PRIMARY KEY,
            application_id bigint NOT NULL REFERENCES applications (id),
            type           text NOT NULL,
            payload        jsonb NOT NULL DEFAULT '{}',
            actor          text NOT NULL CHECK (actor IN ('you', 'workflow', 'email')),
            at             timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX events_application_at ON events (application_id, at)")
    _append_only("events")


def _append_only(table: str) -> None:
    # Row-level for UPDATE and DELETE; statement-level for TRUNCATE, which
    # doesn't fire row triggers.
    op.execute(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    # DROP TABLE fires no row or truncate triggers, so this still works.
    op.execute("DROP TABLE events")
    op.execute("DROP TABLE applications")
    op.execute("DROP FUNCTION applications_guard()")
    op.execute("DROP TABLE jd_snapshots")
    op.execute("DROP FUNCTION forbid_change()")
