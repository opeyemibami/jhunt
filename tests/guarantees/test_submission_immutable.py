"""Guarantee: a submitted document is never lost or altered (ADR 0013).

The same mechanism as snapshots (ADR 0002, ADR 0007):

- **The file**: stored by store_submission_file, as the app identity
  (jhunt-app locally, jhunt-ci in CI, with exactly the app's records roles),
  on the real CI bucket, which is set up like records. It carries the
  records' retention. Deleting, overwriting, changing its retention or
  setting a hold all fail, and a hash mismatch on read is detected.
- **The rows**: documents, submissions and submission_documents refuse
  UPDATE, DELETE and TRUNCATE.
"""

import datetime as dt
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import Forbidden
from google.cloud import storage
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.platform.artifacts import RecordIntegrityError, RecordStore
from jhunt.platform.artifacts.records import RETENTION_MODE, RETENTION_PERIOD
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.workspace.submission import (
    SubmittedFile,
    record_submission,
    store_submission_file_tool,
)

SENT = b"%PDF the CV that was sent"
KEY = "d" * 64


@pytest.fixture
def stored(tmp_path: Path, gcs_test_bucket: str, record_prefix: str) -> dict[str, Any]:
    """A submitted file, kept the way jhunt-submission keeps it."""
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(SENT)
    tool = store_submission_file_tool(RecordStore(gcs_test_bucket), prefix=record_prefix)
    return tool.run(
        {
            "path": str(cv),
            "dedupe_key": KEY,
            "file_name": "cv.pdf",
            "content_type": "application/pdf",
            "sha256": hashlib.sha256(SENT).hexdigest(),
        }
    )


@pytest.fixture
def blob(gcs_test_bucket: str, stored: dict[str, Any]) -> storage.Blob:
    found = storage.Client().bucket(gcs_test_bucket).get_blob(stored["key"])
    assert found is not None
    return found


def unchanged(gcs_test_bucket: str, stored: dict[str, Any]) -> bool:
    data = RecordStore(gcs_test_bucket).get(stored["key"], stored["generation"], stored["sha256"])
    return data == SENT


def test_the_file_carries_the_records_retention(blob: storage.Blob) -> None:
    assert blob.retention.mode == RETENTION_MODE
    until = blob.retention.retain_until_time
    assert until is not None
    assert until > dt.datetime.now(dt.UTC) + RETENTION_PERIOD - dt.timedelta(days=1)


def test_delete_fails(gcs_test_bucket: str, stored: dict[str, Any], blob: storage.Blob) -> None:
    with pytest.raises(Forbidden):
        blob.delete()
    assert unchanged(gcs_test_bucket, stored)


def test_overwrite_fails(gcs_test_bucket: str, stored: dict[str, Any]) -> None:
    # Replacing an object needs storage.objects.delete, which the app lacks.
    replacement = storage.Client().bucket(gcs_test_bucket).blob(stored["key"])
    with pytest.raises(Forbidden):
        replacement.upload_from_string(b"%PDF another CV", content_type="application/pdf")
    assert unchanged(gcs_test_bucket, stored)


def test_retention_cannot_be_shortened_or_cleared(
    gcs_test_bucket: str, stored: dict[str, Any], blob: storage.Blob
) -> None:
    blob.retention.retain_until_time = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    with pytest.raises(Forbidden):
        blob.patch(override_unlocked_retention=True)

    blob.reload()
    blob.retention.mode = None
    blob.retention.retain_until_time = None
    with pytest.raises(Forbidden):
        blob.patch(override_unlocked_retention=True)
    assert unchanged(gcs_test_bucket, stored)


def test_hold_cannot_be_set(blob: storage.Blob) -> None:
    blob.temporary_hold = True
    with pytest.raises(Forbidden):
        blob.patch()


def test_hash_mismatch_is_detected(gcs_test_bucket: str, stored: dict[str, Any]) -> None:
    with pytest.raises(RecordIntegrityError):
        RecordStore(gcs_test_bucket).get(stored["key"], stored["generation"], "f" * 64)


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def submission(engine: Engine) -> int:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url="https://x/1", company="Acme", title="ML Engineer", dedupe_key=KEY,
        snapshot=refs,
    )  # fmt: skip
    cv_ref = {"uri": f"gs://b/submissions/{KEY}/{'1' * 64}/cv.pdf", "generation": 1,
              "sha256": "1" * 64}  # fmt: skip
    submission_id, _ = record_submission(
        engine, submission_key="submission-1-abc", application_id=application_id,
        channel="company site", submitted_at="2026-11-01T00:00:00+00:00",
        files=[SubmittedFile("cv", "cv.pdf", "application/pdf", len(SENT), cv_ref)],
    )  # fmt: skip
    return submission_id


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE documents SET sha256 = repeat('f', 64)",
        "UPDATE documents SET uri = 'gs://elsewhere/cv.pdf'",
        "DELETE FROM documents",
        "TRUNCATE documents CASCADE",
        "UPDATE submissions SET channel = 'email'",
        "UPDATE submissions SET submitted_at = now()",
        "DELETE FROM submissions",
        "TRUNCATE submissions CASCADE",
        "UPDATE submission_documents SET document_id = document_id",
        "DELETE FROM submission_documents",
        "TRUNCATE submission_documents",
    ],
)
def test_the_rows_are_never_changed(engine: Engine, submission: int, statement: str) -> None:
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text(statement))
    with engine.connect() as conn:
        left = conn.execute(
            text(
                "SELECT s.channel, d.sha256 FROM submissions s"
                " JOIN submission_documents sd ON sd.submission_id = s.id"
                " JOIN documents d ON d.id = sd.document_id WHERE s.id = :s"
            ),
            {"s": submission},
        ).one()
    assert tuple(left) == ("company site", "1" * 64)
