"""store_postings: read one board's list from staging, by exact generation and
sha256, and store its postings with their scope (ADR 0011)."""

import hashlib
import json

import pytest
from sqlalchemy import Engine, text

from jhunt.platform.artifacts import RecordIntegrityError, StagingStore
from jhunt.workflows.scout.fit import Targets
from jhunt.workflows.scout.store import store_postings_tool
from tests.workflows.scout.fixtures import GREENHOUSE

TARGETS = Targets(
    title_weight=0.6,
    title_phrases=["platform engineer"],
    include_weight=0.08,
    include_max=0.4,
    include_keywords=["platform"],
    exclude_keywords=["account executive"],
    penalty_weight=0.3,
    penalty_keywords=["staff"],
)


def staged(staging: StagingStore, prefix: str) -> dict[str, object]:
    body = json.dumps(GREENHOUSE).encode()
    record = staging.put(f"{prefix}2026-11-01/greenhouse-acme.json", body, "application/json")
    return {
        "provider": "greenhouse",
        "board": "acme",
        "company": "Acme",
        "northern_ireland_in_scope": False,
        "targets": TARGETS.as_input(),
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
            text(
                "SELECT job_id, in_scope, scope_reason, fit_score, fit_matches"
                " FROM postings ORDER BY job_id"
            )
        ).all()
    assert [tuple(r) for r in stored] == [
        # "Platform Engineer", "Run the platform." in the description
        (
            "101", True, "Dublin", pytest.approx(0.68),
            {"title": ["platform engineer"], "include": ["platform"]},
        ),
        ("102", False, "out: San Francisco, CA", 0.0, {"exclude": ["account executive"]}),
    ]  # fmt: skip
    # A re-run (the gateway normally returns the recorded result) changes nothing.
    assert tool.run(args) == {"new": 0, "seen_again": 2, "in_scope": 1}


def test_a_list_that_changed_in_staging_is_refused(
    db: tuple[Engine, str], staging: StagingStore, record_prefix: str
) -> None:
    engine, _ = db
    args = staged(staging, record_prefix) | {"sha256": "0" * 64}
    with pytest.raises(RecordIntegrityError):
        store_postings_tool(engine, staging).run(args)


def test_a_version_1_thread_passes_no_targets_and_stores_unscored(
    db: tuple[Engine, str], staging: StagingStore, record_prefix: str
) -> None:
    engine, _ = db
    args = staged(staging, record_prefix)
    del args["targets"]
    store_postings_tool(engine, staging).run(args)
    with engine.connect() as conn:
        scores = conn.execute(text("SELECT fit_score, fit_matches FROM postings")).all()
    assert [tuple(r) for r in scores] == [(None, None), (None, None)]
