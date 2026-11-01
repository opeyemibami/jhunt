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

TABLES = [
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


def seed(engine: Engine, n: int) -> int:
    """A captured application with everything Capture leaves: rows in every table."""
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
    assert (plan.bucket, plan.prefix) == ("bkt", f"snapshots/{'1' * 64}/")
    assert plan.threads == [capture.thread_id_for("1" * 64)]
    assert plan.rows == dict.fromkeys(TABLES, 1)
    assert cmd.plan(engine, 9999) is None


def test_delete_removes_exactly_one_application_in_one_transaction(engine: Engine) -> None:
    target = seed(engine, 1)
    seed(engine, 2)  # must be left alone
    plan = cmd.plan(engine, target)
    assert plan is not None

    removed = cmd.delete_rows(engine, plan)

    assert removed == dict.fromkeys(TABLES, 1)
    assert counts(engine) == dict.fromkeys(TABLES, 1)  # only the other application is left
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


def test_command_shows_everything_and_deletes_only_when_the_id_is_typed(
    engine: Engine,
    migrated_db_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = seed(engine, 1)
    generations = [
        Generation(f"snapshots/{'1' * 64}/page.html", 1, True),
        Generation(f"snapshots/{'1' * 64}/page.txt", 2, True),
    ]
    deleted_records: list[tuple[str, str]] = []
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    cmd.get_settings.cache_clear()
    monkeypatch.setattr(cmd, "owner_client", lambda project: object())
    monkeypatch.setattr(cmd, "list_generations", lambda bucket, prefix, client: generations)

    def fake_delete(bucket: str, prefix: str, client: Any) -> list[Generation]:
        deleted_records.append((bucket, prefix))
        return generations

    monkeypatch.setattr(cmd, "delete_all_generations", fake_delete)
    monkeypatch.setattr(sys, "argv", ["jhunt-applications-delete", str(target)])

    # Typing anything but the id deletes nothing.
    monkeypatch.setattr("builtins.input", lambda prompt: "nope")
    with pytest.raises(SystemExit, match="not confirmed; nothing deleted"):
        cmd.main()
    out = capsys.readouterr().out
    assert f"application {target}" in out
    assert "events: 1" in out and "checkpoint_writes: 1" in out
    assert f"gs://bkt/snapshots/{'1' * 64}/page.html#1" in out
    assert counts(engine)["applications"] == 1 and deleted_records == []

    # Typing the id deletes the rows, then the records.
    monkeypatch.setattr("builtins.input", lambda prompt: str(target))
    cmd.main()
    out = capsys.readouterr().out
    assert counts(engine) == dict.fromkeys(TABLES, 0)
    assert deleted_records == [("bkt", f"snapshots/{'1' * 64}/")]
    assert "deleted 2 record generations" in out
    cmd.get_settings.cache_clear()
