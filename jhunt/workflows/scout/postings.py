"""Store postings: every one, in scope or not, refreshed when seen again (ADR 0010).

Postings aren't append-only (design.md: "replaced daily"): a posting seen again
gets its current title, location, scope and fit score (the targets may have
changed), and a new last_seen_at; its first_seen_at is kept. The Scout
workflow removes postings unseen for 30 days (ADR 0011).
"""

import json

from sqlalchemy import Engine, text

from jhunt.workflows.scout.parse import Posting

_UPSERT = text(
    "INSERT INTO postings (dedupe_key, provider, board, job_id, url, title, company,"
    " location, remote, in_scope, scope_reason, fit_score, fit_matches)"
    " VALUES (:dedupe_key, :provider, :board, :job_id, :url, :title, :company,"
    " :location, :remote, :in_scope, :scope_reason, :fit_score, CAST(:fit_matches AS jsonb))"
    " ON CONFLICT (dedupe_key) DO UPDATE SET"
    "  url = EXCLUDED.url, title = EXCLUDED.title, company = EXCLUDED.company,"
    "  location = EXCLUDED.location, remote = EXCLUDED.remote,"
    "  in_scope = EXCLUDED.in_scope, scope_reason = EXCLUDED.scope_reason,"
    "  fit_score = EXCLUDED.fit_score, fit_matches = EXCLUDED.fit_matches,"
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
                    "fit_score": p.fit_score,
                    "fit_matches": None if p.fit_matches is None else json.dumps(p.fit_matches),
                },
            ).one()
            new += int(row.inserted)
    return {"new": new, "seen_again": len(postings) - new}
