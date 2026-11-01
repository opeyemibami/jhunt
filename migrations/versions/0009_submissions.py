"""documents, submissions, submission_documents; autonomy action record_submission

What the owner sent for an application, locked once confirmed (ADR 0013).
documents holds each file's record (create-only in the records bucket) and its
version within its kind; submissions holds what was sent, when and how;
submission_documents links the two. All three are append-only, like
jd_snapshots (ADR 0007): AGENTS.md hard rule 8.

submission_key is the submission's thread id, derived from what was submitted
(ADR 0013), and unique: the same submission recorded twice is one row.

record_submission (level 1) is the autonomy action for storing those files
through the tool gateway (hard rule 2).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE documents (
            id             bigserial PRIMARY KEY,
            application_id bigint NOT NULL REFERENCES applications (id),
            kind           text NOT NULL CHECK (kind IN ('cv', 'cover_note', 'answers', 'other')),
            -- 1, 2, ... within the application and kind; an edit is a new version
            -- whose parent is the one before it (design.md, Document).
            version        integer NOT NULL CHECK (version >= 1),
            parent_version bigint REFERENCES documents (id),
            file_name      text NOT NULL,
            content_type   text NOT NULL,
            size           bigint NOT NULL CHECK (size >= 0),
            uri            text NOT NULL,
            generation     bigint NOT NULL,
            sha256         text NOT NULL,
            created_by     text NOT NULL CHECK (created_by IN ('you', 'draft')),
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (application_id, kind, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE submissions (
            id             bigserial PRIMARY KEY,
            application_id bigint NOT NULL REFERENCES applications (id),
            submission_key text NOT NULL UNIQUE,
            channel        text NOT NULL,
            submitted_at   timestamptz NOT NULL,
            confirmed_at   timestamptz NOT NULL DEFAULT now(),
            confirmed_by   text NOT NULL CHECK (confirmed_by = 'owner')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE submission_documents (
            submission_id bigint NOT NULL REFERENCES submissions (id),
            document_id   bigint NOT NULL REFERENCES documents (id),
            PRIMARY KEY (submission_id, document_id)
        )
        """
    )
    op.execute("CREATE INDEX documents_application ON documents (application_id, kind, version)")
    op.execute("CREATE INDEX submissions_application ON submissions (application_id, id)")
    for table in ("documents", "submissions", "submission_documents"):
        _append_only(table)

    op.execute(
        "INSERT INTO autonomy_settings (action, level, changed_by)"
        " VALUES ('record_submission', 1, 'migration 0009')"
    )


def _append_only(table: str) -> None:
    # As in 0005: row-level for UPDATE and DELETE, statement-level for TRUNCATE.
    op.execute(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    # DROP TABLE fires no row or truncate triggers.
    op.execute("DROP TABLE submission_documents")
    op.execute("DROP TABLE submissions")
    op.execute("DROP TABLE documents")
    # autonomy_settings is append-only; the migration's own row goes with it.
    op.execute("ALTER TABLE autonomy_settings DISABLE TRIGGER autonomy_settings_append_only")
    op.execute(
        "DELETE FROM autonomy_settings"
        " WHERE action = 'record_submission' AND changed_by = 'migration 0009'"
    )
    op.execute("ALTER TABLE autonomy_settings ENABLE TRIGGER autonomy_settings_append_only")
