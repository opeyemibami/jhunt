"""jhunt-applications-delete: remove one application and everything it left
behind, in one transaction, and nothing else.

On a scratch database. The records part is replaced here (it needs the
owner's login); the real deletion is covered by the curator tests.
"""

import json
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.app import applications_delete as cmd
from jhunt.platform.artifacts.curator import Generation
from jhunt.workflows import capture
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.workspace.submission import (
    SubmittedFile,
    record_submission,
    submission_thread_id,
)

TABLES = [
    "labels",
    "pack_requirements",
    "pack_requirement_checks",
    "draft_edits",
    "draft_claims",
    "pack_drafts",
    "submission_documents",
    "submissions",
    "documents",
    "events",
    "applications",
    "jd_snapshots",
    "tool_calls",
    "runs",
    "threads",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
]


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


# Per seeded application: Capture's rows, plus one submission (its event and
# its upload's tool call are the second of each), plus a failed v2 pack thread.
EACH = dict.fromkeys(TABLES, 1) | {"events": 2, "tool_calls": 2, "threads": 2}


def submission_thread(application_id: int) -> str:
    return submission_thread_id(application_id, ["1" * 64], "email", "2026-11-01T00:00:00+00:00")


def pack_thread(application_id: int) -> str:
    return f"pack-{application_id}-1"


def seed(engine: Engine, n: int) -> int:
    """A captured and submitted application: rows in every table."""
    key = f"{n}" * 64
    thread = capture.thread_id_for(key)
    refs = {
        "html": {"uri": f"gs://bkt/snapshots/{key}/page.html", "generation": 1, "sha256": "aa"},
        "text": {"uri": f"gs://bkt/snapshots/{key}/page.txt", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url=f"https://x/{n}", company="Acme", title="Eng", dedupe_key=key, snapshot=refs
    )
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO threads (thread_id, workflow, version) VALUES (:t, 'capture', 1)"),
            {"t": thread},
        )
        conn.execute(
            text(
                "INSERT INTO runs (thread_id, workflow, version, kind, payload, status)"
                " VALUES (:t, 'capture', 1, 'start', '{}', 'succeeded')"
            ),
            {"t": thread},
        )
        conn.execute(
            text(
                "INSERT INTO tool_calls (idempotency_key, thread_id, node, step_key, tool,"
                " args_hash, level, outcome, result)"
                " VALUES (:k, :t, 'snapshot', 'page', 'snapshot_posting', 'h', 1, 'succeeded',"
                " CAST(:r AS jsonb))"
            ),
            {"k": f"key-{n}", "t": thread, "r": json.dumps(refs)},
        )
        conn.execute(
            text(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint)"
                " VALUES (:t, 'c1', '{}')"
            ),
            {"t": thread},
        )
        conn.execute(
            text(
                "INSERT INTO checkpoint_blobs (thread_id, channel, version, type)"
                " VALUES (:t, 'trail', '1', 'empty')"
            ),
            {"t": thread},
        )
        conn.execute(
            text(
                "INSERT INTO checkpoint_writes"
                " (thread_id, checkpoint_id, task_id, idx, channel, blob)"
                " VALUES (:t, 'c1', 'task', 0, 'trail', '')"
            ),
            {"t": thread},
        )
    # A recorded submission, and its upload's tool call on its own thread.
    sub_thread = submission_thread(application_id)
    cv_ref = {"uri": f"gs://bkt/submissions/{key}/{'1' * 64}/cv.pdf", "generation": 3,
              "sha256": "1" * 64}  # fmt: skip
    record_submission(
        engine, submission_key=sub_thread, application_id=application_id, channel="email",
        submitted_at="2026-11-01T00:00:00+00:00",
        files=[SubmittedFile("cv", "cv.pdf", "application/pdf", 3, cv_ref)],
    )  # fmt: skip
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_calls (idempotency_key, thread_id, node, step_key, tool,"
                " args_hash, level, outcome, result)"
                " VALUES (:k, :t, 'store', 'cv', 'store_submission_file', 'h', 1, 'succeeded',"
                " CAST(:r AS jsonb))"
            ),
            {"k": f"sub-key-{n}", "t": sub_thread, "r": json.dumps(cv_ref)},
        )
        # A v2 thread that stored its requirements, then failed before drafting
        # (ADR 0019): found by its threads row.
        failed = f"pack-{application_id}-2"
        conn.execute(
            text(
                "INSERT INTO threads (thread_id, workflow, version)"
                " VALUES (:t, 'application_pack', 2)"
            ),
            {"t": failed},
        )
        conn.execute(
            text(
                "INSERT INTO pack_requirement_checks (thread_id, dropped, provider, model)"
                " VALUES (:t, 1, 'anthropic', 'claude-sonnet-5-5')"
            ),
            {"t": failed},
        )
        conn.execute(
            text(
                "INSERT INTO pack_requirements (thread_id, position, text, quote, kind, match,"
                " fact_ids, why) VALUES (:t, 0, 'CUDA', 'CUDA', 'must', 'gap', '{}', 'None.')"
            ),
            {"t": failed},
        )
        # A pack draft with one claim (ADR 0016).
        draft_id = conn.execute(
            text(
                "INSERT INTO pack_drafts (application_id, thread_id, snapshot_id, step,"
                " provider, model) SELECT id, :t, snapshot_id, 'pack.draft', 'anthropic',"
                " 'claude-sonnet-5-5' FROM applications WHERE id = :a RETURNING id"
            ),
            {"t": pack_thread(application_id), "a": application_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO draft_claims (draft_id, section, position, text, fact_ids,"
                " fact_versions, blocked, problems)"
                " VALUES (:d, 'cv_line', 0, 'Built things', '{}', '{}', true, '{cites no fact}')"
            ),
            {"d": draft_id},
        )
        # An edit of that claim, and the label it left (ADR 0017).
        conn.execute(
            text(
                "INSERT INTO draft_edits (draft_id, round, section, position, version, text,"
                " warnings, created_by) VALUES (:d, 0, 'cv_line', 0, 1, 'Built more', '{}', 'you')"
            ),
            {"d": draft_id},
        )
        conn.execute(
            text(
                "INSERT INTO labels (kind, target, created_by) VALUES ('draft_edited', :t, 'you')"
            ),
            {"t": f"draft:{draft_id}:0:cv_line:0"},
        )
    return application_id


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in TABLES
        }


def test_plan_names_everything_it_will_remove(engine: Engine) -> None:
    target = seed(engine, 1)
    seed(engine, 2)

    plan = cmd.plan(engine, target)

    assert plan is not None
    assert (plan.bucket, plan.prefixes) == (
        "bkt",
        [f"snapshots/{'1' * 64}/", f"submissions/{'1' * 64}/"],
    )
    assert plan.threads == [
        capture.thread_id_for("1" * 64),
        submission_thread(target),
        pack_thread(target),
        f"pack-{target}-2",  # no draft: found by its threads row
    ]
    assert plan.rows == EACH
    assert cmd.plan(engine, 9999) is None


def test_delete_removes_exactly_one_application_in_one_transaction(engine: Engine) -> None:
    target = seed(engine, 1)
    seed(engine, 2)  # must be left alone
    plan = cmd.plan(engine, target)
    assert plan is not None

    removed = cmd.delete_rows(engine, plan)

    assert removed == EACH
    assert counts(engine) == EACH  # only the other application is left
    with engine.connect() as conn:
        left = conn.execute(text("SELECT dedupe_key FROM applications")).scalar_one()
    assert left == "2" * 64


def test_triggers_are_back_on_after_the_delete(engine: Engine) -> None:
    target = seed(engine, 1)
    other = seed(engine, 2)
    plan = cmd.plan(engine, target)
    assert plan is not None
    cmd.delete_rows(engine, plan)

    # The append-only rules hold again for everything else.
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text("DELETE FROM events WHERE application_id = :a"), {"a": other})
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text("DELETE FROM applications WHERE id = :a"), {"a": other})
    for table in (
        "labels",
        "pack_requirements",
        "pack_requirement_checks",
        "draft_edits",
        "draft_claims",
        "pack_drafts",
        "submission_documents",
        "submissions",
        "documents",
    ):
        with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {table}"))


def test_command_shows_everything_and_deletes_only_when_the_id_is_typed(
    engine: Engine,
    migrated_db_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = seed(engine, 1)
    generations = {
        f"snapshots/{'1' * 64}/": [
            Generation(f"snapshots/{'1' * 64}/page.html", 1, True),
            Generation(f"snapshots/{'1' * 64}/page.txt", 2, True),
        ],
        f"submissions/{'1' * 64}/": [
            Generation(f"submissions/{'1' * 64}/{'1' * 64}/cv.pdf", 3, True),
        ],
    }
    deleted_records: list[tuple[str, str]] = []
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    cmd.get_settings.cache_clear()
    monkeypatch.setattr(cmd, "owner_client", lambda project: object())
    monkeypatch.setattr(cmd, "list_generations", lambda bucket, prefix, client: generations[prefix])

    def fake_delete(bucket: str, prefix: str, client: Any) -> list[Generation]:
        deleted_records.append((bucket, prefix))
        return generations[prefix]

    monkeypatch.setattr(cmd, "delete_all_generations", fake_delete)
    monkeypatch.setattr(sys, "argv", ["jhunt-applications-delete", str(target)])

    # Typing anything but the id deletes nothing.
    monkeypatch.setattr("builtins.input", lambda prompt: "nope")
    with pytest.raises(SystemExit, match="not confirmed; nothing deleted"):
        cmd.main()
    out = capsys.readouterr().out
    assert f"application {target}" in out
    assert "events: 2" in out and "checkpoint_writes: 1" in out and "submissions: 1" in out
    assert f"gs://bkt/snapshots/{'1' * 64}/page.html#1" in out
    assert f"gs://bkt/submissions/{'1' * 64}/{'1' * 64}/cv.pdf#3" in out
    assert counts(engine)["applications"] == 1 and deleted_records == []

    # Typing the id deletes the rows, then the records.
    monkeypatch.setattr("builtins.input", lambda prompt: str(target))
    cmd.main()
    out = capsys.readouterr().out
    assert counts(engine) == dict.fromkeys(TABLES, 0)
    assert deleted_records == [
        ("bkt", f"snapshots/{'1' * 64}/"),
        ("bkt", f"submissions/{'1' * 64}/"),
    ]
    assert "deleted 2 record generations under gs://bkt/snapshots/" in out
    assert "deleted 1 record generations under gs://bkt/submissions/" in out
    cmd.get_settings.cache_clear()
