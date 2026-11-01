"""facts: the fact bank, one row per version (ADR 0014)

Every drafted claim cites facts (hard rule 6). A fact is the owner's: imported
from their own TOML file with jhunt-facts. An edit is a new version, and a row
never changes (append-only, like jd_snapshots), so a draft keeps citing the
version it used. Only a fact's latest version, and only when it is verified,
can be cited by a new draft.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE facts (
            id             bigserial PRIMARY KEY,
            -- The owner's own id for the fact, stable across its versions.
            fact_id        text NOT NULL,
            version        integer NOT NULL CHECK (version >= 1),
            kind           text NOT NULL
                           CHECK (kind IN ('role', 'project', 'metric', 'skill', 'education')),
            text           text NOT NULL CHECK (text <> ''),
            evidence       text,
            verified_at    date,
            status         text NOT NULL CHECK (status IN ('draft', 'verified', 'retired')),
            level          text CHECK (level IN ('studied', 'used', 'built', 'production')),
            -- sha256 of the fields: how an import tells unchanged from changed.
            content_sha256 text NOT NULL,
            created_by     text NOT NULL CHECK (created_by = 'owner'),
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (fact_id, version),
            -- What the importer enforces, kept true here too: only skills have a
            -- level, and a verified fact has its evidence and date (a verified
            -- skill, its level).
            CHECK (level IS NULL OR kind = 'skill'),
            CHECK (status <> 'verified'
                   OR (evidence IS NOT NULL AND verified_at IS NOT NULL
                       AND (kind <> 'skill' OR level IS NOT NULL)))
        )
        """
    )
    # Row-level for UPDATE and DELETE, statement-level for TRUNCATE (as in 0005).
    op.execute(
        "CREATE TRIGGER facts_append_only BEFORE UPDATE OR DELETE ON facts"
        " FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER facts_no_truncate BEFORE TRUNCATE ON facts"
        " FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE facts")  # DROP fires no row or truncate triggers
