"""Guarantee: a captured job description is never lost or altered.

As the app identity (jhunt-app locally, jhunt-ci in CI, with exactly the app's
records roles), on the real CI bucket, which is set up like records:
deleting, overwriting, shortening or clearing retention and setting a hold all
fail, and a hash mismatch on read is detected (ADR 0002).
"""

import datetime as dt

import pytest
from google.api_core.exceptions import Forbidden
from google.cloud import storage

from jhunt.platform.artifacts import RecordIntegrityError, RecordStore, StoredRecord

ORIGINAL = b"<html>Senior Engineer, Dublin</html>"


@pytest.fixture
def record(gcs_test_bucket: str, gcs_client: storage.Client, record_prefix: str) -> StoredRecord:
    return RecordStore(gcs_test_bucket, gcs_client).put(
        f"{record_prefix}snapshot.html", ORIGINAL, "text/html"
    )


@pytest.fixture
def blob(gcs_bucket: storage.Bucket, record: StoredRecord) -> storage.Blob:
    found = gcs_bucket.get_blob(record.key)
    assert found is not None
    return found


def unchanged(gcs_bucket: storage.Bucket, record: StoredRecord) -> bool:
    store = RecordStore(gcs_bucket.name, gcs_bucket.client)
    return store.get(record.key, record.generation, record.sha256) == ORIGINAL


def test_delete_fails(gcs_bucket: storage.Bucket, record: StoredRecord, blob: storage.Blob) -> None:
    with pytest.raises(Forbidden):
        blob.delete()
    assert unchanged(gcs_bucket, record)


def test_overwrite_fails(
    gcs_bucket: storage.Bucket, record: StoredRecord, blob: storage.Blob
) -> None:
    # Replacing an object needs storage.objects.delete, which the app lacks.
    replacement = gcs_bucket.blob(record.key)
    with pytest.raises(Forbidden):
        replacement.upload_from_string(b"<html>altered</html>", content_type="text/html")
    assert unchanged(gcs_bucket, record)


def test_retention_cannot_be_shortened_or_cleared(
    gcs_bucket: storage.Bucket, record: StoredRecord, blob: storage.Blob
) -> None:
    blob.retention.retain_until_time = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    with pytest.raises(Forbidden):
        blob.patch(override_unlocked_retention=True)

    blob.reload()
    blob.retention.mode = None
    blob.retention.retain_until_time = None
    with pytest.raises(Forbidden):
        blob.patch(override_unlocked_retention=True)
    assert unchanged(gcs_bucket, record)


def test_hold_cannot_be_set(gcs_test_bucket: str, record: StoredRecord, blob: storage.Blob) -> None:
    blob.temporary_hold = True
    with pytest.raises(Forbidden):
        blob.patch()


def test_hash_mismatch_is_detected(gcs_bucket: storage.Bucket, record: StoredRecord) -> None:
    store = RecordStore(gcs_bucket.name, gcs_bucket.client)
    altered_hash = "f" * 64  # what a row would hold if the bytes and the hash disagreed
    with pytest.raises(RecordIntegrityError):
        store.get(record.key, record.generation, altered_hash)
