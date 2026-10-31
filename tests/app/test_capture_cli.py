"""jhunt-capture queues a posting once, and says so when it's queued or captured."""

import sys
from collections.abc import Iterator

import pytest
from sqlalchemy import URL, create_engine, pool, text

from jhunt.app import capture as capture_cli
from jhunt.workflows import capture
from jhunt.workflows.capture.record import record_capture

URL_ = "https://jobs.example/42?utm_source=x"


@pytest.fixture
def scratch_env(
    monkeypatch: pytest.MonkeyPatch, migrated_db_url: URL, gcs_test_bucket: str
) -> Iterator[URL]:
    # The command reads settings from the environment: point it at the scratch
    # database and the CI bucket, never the dev database or real records.
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCS_RECORDS_BUCKET", gcs_test_bucket)
    capture_cli.get_settings.cache_clear()
    yield migrated_db_url
    capture_cli.get_settings.cache_clear()


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr(
        sys, "argv", ["jhunt-capture", URL_, "--company", "Acme", "--title", "Engineer"]
    )
    capture_cli.main()
    return capsys.readouterr().out.strip()


def test_queues_once_then_says_queued_then_captured(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = capture.dedupe_key(URL_, "Acme", "Engineer")
    thread_id = capture.thread_id_for(key)

    assert run(monkeypatch, capsys) == f"queued: thread {thread_id}"
    assert run(monkeypatch, capsys) == f"already queued: thread {thread_id}"

    engine = create_engine(scratch_env, poolclass=pool.NullPool)
    with engine.connect() as conn:
        queued = conn.execute(
            text("SELECT kind, payload FROM runs WHERE thread_id = :t"), {"t": thread_id}
        ).all()
    assert [(r.kind, r.payload["dedupe_key"]) for r in queued] == [("start", key)]

    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url=URL_, company="Acme", title="Engineer", dedupe_key=key, snapshot=refs
    )
    engine.dispose()
    assert run(monkeypatch, capsys) == f"already captured: application {application_id}"
