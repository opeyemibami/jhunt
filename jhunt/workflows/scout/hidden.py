"""Postings the owner isn't interested in (owner, UI v1).

"Not interested" appends a skip label for the posting's dedupe_key; Undo
appends an unskip. The latest of the two decides, so a posting stays hidden
when Scout sees it again, and labels are never changed (migration 0016).
"""

import json
from typing import Any

from sqlalchemy import Connection, Engine, text

# The latest skip or unskip for each target, where it's a skip: what's hidden.
_HIDDEN = (
    "SELECT DISTINCT ON (target) target, kind, detail, at FROM labels"
    " WHERE kind IN ('skip', 'unskip') ORDER BY target, id DESC"
)


def hide_posting(engine: Engine, posting_id: int) -> bool:
    """Hide this posting for good. False if there's no such posting."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT dedupe_key, title, company, url FROM postings WHERE id = :id"),
            {"id": posting_id},
        ).first()
        if row is None:
            return False
        if row.dedupe_key not in hidden_keys(conn):
            detail = {"title": row.title, "company": row.company, "url": row.url}
            _label(conn, "skip", row.dedupe_key, detail)
    return True


def unhide(engine: Engine, dedupe_key: str) -> bool:
    """Undo a hide. False if that posting isn't hidden."""
    with engine.begin() as conn:
        if dedupe_key not in hidden_keys(conn):
            return False
        _label(conn, "unskip", dedupe_key, {})
    return True


def hidden_keys(conn: Connection) -> set[str]:
    return {r.target for r in conn.execute(text(_HIDDEN)) if r.kind == "skip"}


def hidden_postings(engine: Engine) -> list[Any]:
    """Each hidden posting: what was hidden, when, and the posting if it's still stored."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    f"SELECT h.target, h.detail, h.at, p.id AS posting_id, p.location"
                    f" FROM ({_HIDDEN}) h LEFT JOIN postings p ON p.dedupe_key = h.target"
                    " WHERE h.kind = 'skip' ORDER BY h.at DESC"
                )
            )
        )


def _label(conn: Connection, kind: str, target: str, detail: dict[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO labels (kind, target, detail, created_by)"
            " VALUES (:k, :t, CAST(:d AS jsonb), 'you')"
        ),
        {"k": kind, "t": target, "d": json.dumps(detail)},
    )
