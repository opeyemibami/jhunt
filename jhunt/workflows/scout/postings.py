"""Store postings: every one, in scope or not, refreshed when seen again (ADR 0010).

Postings aren't append-only (design.md: "replaced daily"): a posting seen again
gets its current title, location and scope, and a new last_seen_at; its
first_seen_at and fit_score are kept. Removing postings unseen for 30 days
comes with the Scout workflow (PR 3.2).
"""

from sqlalchemy import Engine, text

from jhunt.workflows.scout.parse import Posting

_UPSERT = text(
    "INSERT INTO postings (dedupe_key, provider, board, job_id, url, title, company,"
    " location, remote, in_scope, scope_reason)"
    " VALUES (:dedupe_key, :provider, :board, :job_id, :url, :title, :company,"
    " :location, :remote, :in_scope, :scope_reason)"
    " ON CONFLICT (dedupe_key) DO UPDATE SET"
    "  url = EXCLUDED.url, title = EXCLUDED.title, company = EXCLUDED.company,"
    "  location = EXCLUDED.location, remote = EXCLUDED.remote,"
    "  in_scope = EXCLUDED.in_scope, scope_reason = EXCLUDED.scope_reason,"
    "  last_seen_at = now()"
    # xmax = 0 only on a freshly inserted row: how new and refreshed are told apart.
    " RETURNING (xmax = 0) AS inserted"
)


def upsert_postings(engine: Engine, postings: list[Posting]) -> dict[str, int]:
    """Insert new postings and refresh known ones, in one transaction."""
    new = 0
    with engine.begin() as conn:
        for p in postings:
            row = conn.execute(
                _UPSERT,
                {
                    "dedupe_key": p.dedupe_key,
                    "provider": p.provider,
                    "board": p.board,
                    "job_id": p.job_id,
                    "url": p.url,
                    "title": p.title,
                    "company": p.company,
                    "location": p.location,
                    "remote": p.remote,
                    "in_scope": p.in_scope,
                    "scope_reason": p.scope_reason,
                },
            ).one()
            new += int(row.inserted)
    return {"new": new, "seen_again": len(postings) - new}
