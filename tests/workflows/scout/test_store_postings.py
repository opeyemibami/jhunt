"""store_postings: read one board's list from staging, by exact generation and
sha256, and store its postings with their scope (ADR 0011)."""

import hashlib
import json

import pytest
from sqlalchemy import Engine, text

from jhunt.platform.artifacts import RecordIntegrityError, StagingStore
from jhunt.workflows.scout.store import store_postings_tool
from tests.workflows.scout.fixtures import GREENHOUSE


def staged(staging: StagingStore, prefix: str) -> dict[str, object]:
    body = json.dumps(GREENHOUSE).encode()
    record = staging.put(f"{prefix}2026-11-01/greenhouse-acme.json", body, "application/json")
    return {
        "provider": "greenhouse",
        "board": "acme",
        "company": "Acme",
        "northern_ireland_in_scope": False,
        "key": record.key,
        "generation": record.generation,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def test_stores_the_boards_postings_with_their_scope(
    db: tuple[Engine, str], staging: StagingStore, record_prefix: str
) -> None:
    engine, _ = db
    tool = store_postings_tool(engine, staging)
    assert tool.action == "fetch_postings"  # "fetch, dedupe, score postings": level 1

    args = staged(staging, record_prefix)
    assert tool.run(args) == {"new": 2, "seen_again": 0, "in_scope": 1}
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT job_id, in_scope, scope_reason FROM postings ORDER BY job_id")
        ).all()
    assert [tuple(r) for r in stored] == [
        ("101", True, "Dublin"),
        ("102", False, "out: San Francisco, CA"),
    ]
    # A re-run (the gateway normally returns the recorded result) changes nothing.
    assert tool.run(args) == {"new": 0, "seen_again": 2, "in_scope": 1}


def test_a_list_that_changed_in_staging_is_refused(
    db: tuple[Engine, str], staging: StagingStore, record_prefix: str
) -> None:
    engine, _ = db
    args = staged(staging, record_prefix) | {"sha256": "0" * 64}
    with pytest.raises(RecordIntegrityError):
        store_postings_tool(engine, staging).run(args)
