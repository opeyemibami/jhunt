"""Record a capture: the snapshot row, the application and its first event.

All three are written in one transaction, or none are. Snapshot rows can never
be deleted (ADR 0007), so this checks for the application first and writes
nothing if it exists. That keeps a re-run of the step from leaving a stray row.
"""

import json
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError


def record_capture(
    engine: Engine,
    *,
    url: str,
    company: str,
    title: str,
    dedupe_key: str,
    snapshot: dict[str, Any],
) -> tuple[int, bool]:
    """Returns (application id, already_captured)."""
    existing = _application_id(engine, dedupe_key)
    if existing is not None:
        return existing, True
    try:
        with engine.begin() as conn:
            snapshot_id = conn.execute(
                text(
                    "INSERT INTO jd_snapshots (url, text_uri, text_generation, text_sha256,"
                    " html_uri, html_generation, html_sha256)"
                    " VALUES (:url, :text_uri, :text_generation, :text_sha256,"
                    " :html_uri, :html_generation, :html_sha256) RETURNING id"
                ),
                {
                    "url": url,
                    "text_uri": snapshot["text"]["uri"],
                    "text_generation": snapshot["text"]["generation"],
                    "text_sha256": snapshot["text"]["sha256"],
                    "html_uri": snapshot["html"]["uri"],
                    "html_generation": snapshot["html"]["generation"],
                    "html_sha256": snapshot["html"]["sha256"],
                },
            ).scalar_one()
            application_id: int = conn.execute(
                text(
                    "INSERT INTO applications (dedupe_key, snapshot_id, company, role, stage)"
                    " VALUES (:key, :snapshot_id, :company, :role, 'captured') RETURNING id"
                ),
                {"key": dedupe_key, "snapshot_id": snapshot_id, "company": company, "role": title},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO events (application_id, type, payload, actor)"
                    " VALUES (:id, 'captured', CAST(:payload AS jsonb), 'workflow')"
                ),
                {"id": application_id, "payload": json.dumps({"snapshot_id": snapshot_id})},
            )
        return application_id, False
    except IntegrityError:
        # Another capture of the same posting committed first: the unique
        # dedupe_key refused ours, and the whole transaction rolled back.
        winner = _application_id(engine, dedupe_key)
        if winner is None:
            raise  # some other integrity problem: don't hide it
        return winner, True


def _application_id(engine: Engine, dedupe_key: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            text("SELECT id FROM applications WHERE dedupe_key = :key"), {"key": dedupe_key}
        ).scalar_one_or_none()
    return found
