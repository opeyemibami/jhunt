"""Recording a submission (ADR 0013): the files the owner sent are kept as
records, and the submission, its documents and their links are locked."""

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from google.cloud import storage
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import ToolGateway
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.workspace.submission import (
    SubmissionFileChanged,
    SubmittedFile,
    record_submission,
    store_submission_file_tool,
    submission_thread_id,
)

KEY = "a" * 64


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def application(engine: Engine) -> int:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url="https://x/1", company="Acme", title="ML Engineer", dedupe_key=KEY,
        snapshot=refs,
    )  # fmt: skip
    return application_id


def file_args(path: Path, prefix_key: str = KEY) -> dict[str, Any]:
    return {
        "path": str(path),
        "dedupe_key": prefix_key,
        "file_name": path.name,
        "content_type": "application/pdf",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_a_file_is_stored_create_only_under_its_sha256(
    tmp_path: Path, gcs_test_bucket: str, gcs_client: storage.Client, record_prefix: str
) -> None:
    store = RecordStore(gcs_test_bucket, gcs_client)
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF my cv")
    tool = store_submission_file_tool(store, prefix=record_prefix)
    assert (tool.name, tool.action) == ("store_submission_file", "record_submission")

    args = file_args(cv)
    ref = tool.run(args)
    assert ref["key"] == f"{record_prefix}{KEY}/{args['sha256']}/cv.pdf"
    assert store.get(ref["key"], ref["generation"], ref["sha256"]) == b"%PDF my cv"
    assert tool.run(args) == ref  # the same bytes: adopted, not written again


def test_a_file_changed_after_confirming_is_refused(
    tmp_path: Path, gcs_test_bucket: str, gcs_client: storage.Client, record_prefix: str
) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF confirmed")
    args = file_args(cv)
    cv.write_bytes(b"%PDF edited after the owner confirmed")
    with pytest.raises(SubmissionFileChanged):
        store_submission_file_tool(
            RecordStore(gcs_test_bucket, gcs_client), prefix=record_prefix
        ).run(args)


@pytest.mark.parametrize(
    "change",
    [{"file_name": "../cv.pdf"}, {"file_name": ""}, {"dedupe_key": "x/y"}, {"sha256": "nothex"}],
)
def test_arguments_that_become_the_object_name_are_checked(
    tmp_path: Path,
    gcs_test_bucket: str,
    gcs_client: storage.Client,
    record_prefix: str,
    change: dict[str, str],
) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    tool = store_submission_file_tool(
        RecordStore(gcs_test_bucket, gcs_client), prefix=record_prefix
    )
    with pytest.raises(ValueError):
        tool.run(file_args(cv) | change)


def ref(sha: str) -> dict[str, Any]:
    return {"uri": f"gs://b/submissions/{KEY}/{sha}/f", "generation": 1, "sha256": sha}


def submitted(kind: str, name: str, sha: str) -> SubmittedFile:
    return SubmittedFile(kind, name, "application/pdf", 100, ref(sha))


def rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)))


def test_a_submission_locks_its_documents_and_moves_the_application(
    engine: Engine, application: int
) -> None:
    submission_id, document_ids = record_submission(
        engine,
        submission_key="submission-1-first",
        application_id=application,
        channel="company site",
        submitted_at="2026-11-01T09:30:00+00:00",
        files=[submitted("cv", "cv.pdf", "1" * 64), submitted("answers", "answers.md", "2" * 64)],
    )

    docs = rows(engine, "SELECT id, kind, version, parent_version, created_by, file_name,"
                " sha256 FROM documents ORDER BY id")  # fmt: skip
    assert [(d.kind, d.version, d.parent_version, d.created_by, d.file_name) for d in docs] == [
        ("cv", 1, None, "you", "cv.pdf"),
        ("answers", 1, None, "you", "answers.md"),
    ]
    [sub] = rows(
        engine, "SELECT id, submission_key, channel, submitted_at, confirmed_by FROM submissions"
    )
    assert (sub.id, sub.submission_key, sub.channel, sub.confirmed_by) == (
        submission_id,
        "submission-1-first",
        "company site",
        "owner",
    )
    assert sub.submitted_at.isoformat() == "2026-11-01T09:30:00+00:00"
    links = rows(engine, "SELECT document_id FROM submission_documents ORDER BY document_id")
    assert [link.document_id for link in links] == document_ids == [d.id for d in docs]
    [app] = rows(engine, "SELECT stage FROM applications")
    assert app.stage == "submitted"
    [event] = rows(engine, "SELECT type, payload, actor FROM events WHERE type <> 'captured'")
    assert (event.type, event.actor) == ("submission.confirmed", "you")
    assert event.payload == {
        "submission_id": submission_id,
        "document_ids": document_ids,
        "channel": "company site",
    }


def test_a_later_submission_reuses_unchanged_files_and_versions_changed_ones(
    engine: Engine, application: int
) -> None:
    first, [cv1] = record_submission(
        engine, submission_key="k1", application_id=application, channel="email",
        submitted_at="2026-11-01T09:00:00+00:00", files=[submitted("cv", "cv.pdf", "1" * 64)],
    )  # fmt: skip
    # The same CV again, plus a cover note: the CV is the same document.
    second, [cv_again, note] = record_submission(
        engine, submission_key="k2", application_id=application, channel="email",
        submitted_at="2026-11-02T09:00:00+00:00",
        files=[submitted("cv", "cv.pdf", "1" * 64), submitted("cover_note", "note.pdf", "3" * 64)],
    )  # fmt: skip
    assert cv_again == cv1 and note != cv1
    # A changed CV is a new version of the CV, with the earlier one as parent.
    third, [cv2] = record_submission(
        engine, submission_key="k3", application_id=application, channel="email",
        submitted_at="2026-11-03T09:00:00+00:00", files=[submitted("cv", "cv-v2.pdf", "4" * 64)],
    )  # fmt: skip
    [doc] = rows(engine, f"SELECT kind, version, parent_version FROM documents WHERE id = {cv2}")
    assert tuple(doc) == ("cv", 2, cv1)
    assert len({first, second, third}) == 3
    assert len(rows(engine, "SELECT 1 FROM submissions")) == 3  # none replaced


def test_recording_the_same_submission_again_returns_it_unchanged(
    engine: Engine, application: int
) -> None:
    # A re-run after a crash between the commit and the command's last line.
    files = [submitted("cv", "cv.pdf", "1" * 64), submitted("answers", "a.md", "2" * 64)]
    first = record_submission(
        engine, submission_key="submission-1-abc", application_id=application,
        channel="email", submitted_at="2026-11-01T00:00:00+00:00", files=files,
    )  # fmt: skip
    again = record_submission(
        engine, submission_key="submission-1-abc", application_id=application,
        channel="email", submitted_at="2026-11-01T00:00:00+00:00", files=files,
    )  # fmt: skip
    assert again == first
    assert len(rows(engine, "SELECT 1 FROM submissions")) == 1
    assert len(rows(engine, "SELECT 1 FROM documents")) == 2
    assert len(rows(engine, "SELECT 1 FROM events WHERE type = 'submission.confirmed'")) == 1


def test_the_thread_id_comes_from_what_was_submitted_not_a_counter() -> None:
    a, b = "a" * 64, "b" * 64
    base = submission_thread_id(7, [a, b], "email", "2026-11-01T00:00:00+00:00")
    assert re.fullmatch(r"submission-7-[0-9a-f]{12}", base)
    # The order the files were given in doesn't matter.
    assert submission_thread_id(7, [b, a], "email", "2026-11-01T00:00:00+00:00") == base
    # Anything else does.
    others = {
        submission_thread_id(8, [a, b], "email", "2026-11-01T00:00:00+00:00"),
        submission_thread_id(7, [a], "email", "2026-11-01T00:00:00+00:00"),
        submission_thread_id(7, [a, b], "company site", "2026-11-01T00:00:00+00:00"),
        submission_thread_id(7, [a, b], "email", "2026-11-02T00:00:00+00:00"),
    }
    assert base not in others and len(others) == 4


@pytest.mark.parametrize("table", ["documents", "submissions", "submission_documents"])
def test_submission_rows_never_change(engine: Engine, application: int, table: str) -> None:
    record_submission(
        engine, submission_key="k1", application_id=application, channel="email",
        submitted_at="2026-11-01T09:00:00+00:00", files=[submitted("cv", "cv.pdf", "1" * 64)],
    )  # fmt: skip
    for sql in (f"DELETE FROM {table}", f"TRUNCATE {table} CASCADE"):
        with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
            conn.execute(text(sql))
    column = "submission_id" if table == "submission_documents" else "id"
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET {column} = {column}"))


def test_the_gateway_runs_the_file_tool_at_level_1(
    engine: Engine,
    tmp_path: Path,
    gcs_test_bucket: str,
    gcs_client: storage.Client,
    record_prefix: str,
) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    gateway = ToolGateway(
        engine,
        [
            store_submission_file_tool(
                RecordStore(gcs_test_bucket, gcs_client), prefix=record_prefix
            )
        ],
    )
    result = gateway.call("submission-1-1", "store", "cv", "store_submission_file",
                          file_args(cv), run_id=None)  # fmt: skip
    [call] = rows(engine, "SELECT tool, level FROM tool_calls")
    assert tuple(call) == ("store_submission_file", 1)
    assert result["sha256"] == hashlib.sha256(b"%PDF").hexdigest()
