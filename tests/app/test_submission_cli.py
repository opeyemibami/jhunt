"""jhunt-submission records what the owner sent, once they type the id back
(ADR 0013). Re-running the same command, even after a crash, records it once."""

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from google.cloud import storage
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.app import submission as cmd
from jhunt.workflows.capture.record import record_capture

KEY = "c" * 64


@pytest.fixture
def engine(
    monkeypatch: pytest.MonkeyPatch, migrated_db_url: URL, gcs_test_bucket: str, record_prefix: str
) -> Iterator[Engine]:
    # The scratch database and the CI bucket under this test's prefix: never
    # the dev database or the real records bucket.
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCS_RECORDS_BUCKET", gcs_test_bucket)
    monkeypatch.setenv("GCS_STAGING_BUCKET", gcs_test_bucket)
    monkeypatch.setattr(cmd, "RECORDS_PREFIX", record_prefix)
    monkeypatch.setattr(cmd, "today", lambda: "2026-11-01")
    cmd.get_settings.cache_clear()
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()
    cmd.get_settings.cache_clear()


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


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    (tmp_path / "cv.pdf").write_bytes(b"%PDF the CV I sent")
    (tmp_path / "answers.md").write_text("Why us?\nBecause.\n")
    return {"cv": tmp_path / "cv.pdf", "answers": tmp_path / "answers.md"}


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str,
        typed: str | None) -> str:  # fmt: skip
    """Run the command; typed is what the owner types at the prompt (None: no prompt)."""

    def answer(prompt: str) -> str:
        if typed is None:
            pytest.fail(f"unexpected prompt: {prompt}")
        return typed

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(sys, "argv", ["jhunt-submission", *args])
    cmd.main()
    return capsys.readouterr().out


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def stored(bucket: storage.Bucket, prefix: str) -> list[str]:
    return sorted(b.name for b in bucket.client.list_blobs(bucket, prefix=prefix))


def args_for(application: int, files: dict[str, Path]) -> list[str]:
    return [str(application), "--cv", str(files["cv"]), "--answers", str(files["answers"]),
            "--channel", "company site"]  # fmt: skip


def test_records_the_files_once_the_id_is_typed(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], gcs_bucket: storage.Bucket, record_prefix: str,
) -> None:  # fmt: skip
    out = run(monkeypatch, capsys, *args_for(application, files), typed=str(application))
    assert f"application {application}: Acme, ML Engineer" in out
    assert "cv: cv.pdf (18 bytes, sha256 " in out and "answers: answers.md" in out
    assert "channel: company site" in out and "submitted: 2026-11-01" in out
    assert f"recorded: submission 1 (application {application}, 2 documents)" in out
    assert (count(engine, "submissions"), count(engine, "documents")) == (1, 2)
    assert count(engine, "tool_calls") == 2
    names = stored(gcs_bucket, record_prefix)
    assert len(names) == 2 and all(n.startswith(f"{record_prefix}{KEY}/") for n in names)


def test_nothing_is_stored_unless_the_id_is_typed(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], gcs_bucket: storage.Bucket, record_prefix: str,
) -> None:  # fmt: skip
    with pytest.raises(SystemExit, match="not confirmed; nothing recorded"):
        run(monkeypatch, capsys, *args_for(application, files), typed="nope")
    assert count(engine, "submissions") == count(engine, "tool_calls") == 0
    assert stored(gcs_bucket, record_prefix) == []


def test_the_same_command_after_a_crash_records_once(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    # The first attempt dies after the uploads, before the submission is written.
    real = cmd.record_submission

    def crash(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("killed")

    monkeypatch.setattr(cmd, "record_submission", crash)
    with pytest.raises(RuntimeError, match="killed"):
        run(monkeypatch, capsys, *args_for(application, files), typed=str(application))
    assert (count(engine, "tool_calls"), count(engine, "submissions")) == (2, 0)

    # The same command again: the same thread, so the uploads come back from
    # the gateway's record, and the submission is written once.
    monkeypatch.setattr(cmd, "record_submission", real)
    out = run(monkeypatch, capsys, *args_for(application, files), typed=str(application))
    assert "recorded: submission 1" in out
    assert (count(engine, "tool_calls"), count(engine, "submissions")) == (2, 1)
    assert count(engine, "documents") == 2

    # And once more, after it succeeded: nothing new, and no prompt.
    out = run(monkeypatch, capsys, *args_for(application, files), typed=None)
    assert "already recorded: submission 1" in out
    assert (count(engine, "submissions"), count(engine, "documents")) == (1, 2)


def test_a_later_submission_shows_the_earlier_ones_first(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, *args_for(application, files), typed=str(application))
    out = run(monkeypatch, capsys, str(application), "--cv", str(files["cv"]),
              "--channel", "email", typed=str(application))  # fmt: skip
    assert "earlier: submission 1, 2026-11-01 via company site" in out
    assert "recorded: submission 2" in out
    assert count(engine, "documents") == 2  # the same CV is the same document


def test_home_is_expanded(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    # Storage credentials are found through HOME (~/.config/gcloud): keep
    # pointing at them, wherever HOME goes. CI's come from an env variable.
    gcloud = os.environ.get("CLOUDSDK_CONFIG", str(Path.home() / ".config" / "gcloud"))
    monkeypatch.setenv("CLOUDSDK_CONFIG", gcloud)
    monkeypatch.setenv("HOME", str(files["cv"].parent))
    out = run(monkeypatch, capsys, str(application), "--cv", "~/cv.pdf", "--channel", "email",
              typed=str(application))  # fmt: skip
    assert "cv: cv.pdf" in out and "recorded: submission 1" in out


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (["--cv", r"C:\Users\me\cv.pdf"],
         r"looks like a Windows path; in WSL use /mnt/c/Users/me/cv.pdf"),
        (["--cv", "/no/such/cv.pdf"], "no such file: /no/such/cv.pdf"),
        (["--cv", "{dir}/cv.exe"], "cv.exe: not a PDF, DOCX, ODT, TXT or MD file"),
        (["--answers", "{dir}/cv.pdf"], "cv.pdf: answers must be a TXT or MD file"),
        (["--cv", "{dir}/cv.pdf", "--other", "{dir}/cv.pdf"], "the same file is given twice"),
        (["--submitted-at", "2999-01-01"], "is in the future"),
        (["--submitted-at", "yesterday"], "--submitted-at: use 2026-11-01 or 2026-11-01T09:30"),
        ([], "nothing to record: give at least one file"),
    ],
)  # fmt: skip
def test_bad_input_is_refused_before_anything_is_stored(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], change: list[str], error: str,
) -> None:  # fmt: skip
    folder = files["cv"].parent
    (folder / "cv.exe").write_bytes(b"MZ")
    extra = [part.replace("{dir}", str(folder)) for part in change]
    given = [] if change == [] or "--cv" in change else ["--cv", str(files["cv"])]
    with pytest.raises(SystemExit, match=f"jhunt-submission: .*{re.escape(error)}"):
        run(monkeypatch, capsys, str(application), *given, *extra, "--channel", "email",
            typed=None)  # fmt: skip
    assert count(engine, "tool_calls") == count(engine, "submissions") == 0


def test_a_file_over_the_limit_is_refused(
    engine: Engine, application: int, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setattr(cmd, "MAX_FILE_BYTES", 10)
    with pytest.raises(SystemExit, match=re.escape("cv.pdf: 18 bytes, over the 10-byte limit")):
        run(monkeypatch, capsys, str(application), "--cv", str(files["cv"]), "--channel", "email",
            typed=None)  # fmt: skip


def test_an_unknown_application_is_refused(
    engine: Engine, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    with pytest.raises(SystemExit, match="jhunt-submission: no application 999"):
        run(monkeypatch, capsys, "999", "--cv", str(files["cv"]), "--channel", "email", typed=None)
