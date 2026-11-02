"""pack_drafts, draft_claims: application pack drafts and every claim in them (ADR 0016)

A draft is what the model wrote for an application, after the claim validator
(ADR 0015) checked it. Every claim is kept, accepted or blocked with its
reasons, with its text exactly as drafted: blocked claims are listed, never
reworded (hard rule 6). Both tables are append-only; a draft is never edited,
only followed by another.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pack_drafts (
            id             bigserial PRIMARY KEY,
            application_id bigint NOT NULL REFERENCES applications (id),
            -- The Application pack thread that drafted it: one draft per thread.
            thread_id      text NOT NULL UNIQUE,
            snapshot_id    bigint NOT NULL REFERENCES jd_snapshots (id),
            step           text NOT NULL,  -- the model route, pack.draft
            provider       text NOT NULL,
            model          text NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE draft_claims (
            draft_id      bigint NOT NULL REFERENCES pack_drafts (id),
            section       text NOT NULL CHECK (section IN ('cv_line', 'cover_note')),
            position      integer NOT NULL CHECK (position >= 0),
            text          text NOT NULL,  -- exactly as drafted
            fact_ids      text[] NOT NULL,  -- as cited
            fact_versions bigint[] NOT NULL,  -- the facts rows it was checked against
            blocked       boolean NOT NULL,
            problems      text[] NOT NULL,
            PRIMARY KEY (draft_id, section, position),
            CHECK (blocked = (cardinality(problems) > 0))
        )
        """
    )
    op.execute("CREATE INDEX pack_drafts_application ON pack_drafts (application_id, id)")
    for table in ("pack_drafts", "draft_claims"):
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
    op.execute("DROP TABLE draft_claims")  # DROP fires no row or truncate triggers
    op.execute("DROP TABLE pack_drafts")
