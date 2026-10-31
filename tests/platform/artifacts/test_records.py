"""Records on the real CI bucket, as the app identity (jhunt-app locally, jhunt-ci in CI)."""

import datetime as dt
import inspect

import pytest
from google.cloud import storage

from jhunt.platform.artifacts import RecordExists, RecordIntegrityError, RecordStore
from jhunt.platform.artifacts.records import RETENTION_PERIOD


@pytest.fixture
def store(gcs_test_bucket: str) -> RecordStore:
    return RecordStore(gcs_test_bucket)


def test_every_record_is_unlocked_for_three_years(
    store: RecordStore, gcs_test_bucket: str, record_prefix: str
) -> None:
    before = dt.datetime.now(dt.UTC)
    rec = store.put(f"{record_prefix}a.txt", b"hello", "text/plain")

    blob = storage.Client().bucket(gcs_test_bucket).get_blob(rec.key, generation=rec.generation)
    assert blob is not None
    assert blob.retention.mode == "Unlocked"
    until = blob.retention.retain_until_time
    assert before + RETENTION_PERIOD - dt.timedelta(minutes=1) <= until
    assert until <= dt.datetime.now(dt.UTC) + RETENTION_PERIOD + dt.timedelta(minutes=1)
    assert dt.timedelta(days=3 * 365) <= RETENTION_PERIOD <= dt.timedelta(days=3 * 365 + 1)


def test_callers_cannot_choose_the_retention() -> None:
    # The app must never pick a Locked mode or another date (ADR 0002).
    assert list(inspect.signature(RecordStore.put).parameters) == [
        "self",
        "key",
        "data",
        "content_type",
    ]


def test_put_records_the_sha256_and_get_checks_it(
    store: RecordStore, gcs_test_bucket: str, record_prefix: str
) -> None:
    rec = store.put(f"{record_prefix}a.txt", b"hello", "text/plain")
    assert rec.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert rec.uri == f"gs://{gcs_test_bucket}/{record_prefix}a.txt"
    assert (rec.size, rec.generation > 0) == (5, True)

    blob = storage.Client().bucket(gcs_test_bucket).get_blob(rec.key)
    assert blob is not None and blob.metadata == {"sha256": rec.sha256}
    assert store.get(rec.key, rec.generation, rec.sha256) == b"hello"


def test_put_is_create_only(store: RecordStore, record_prefix: str) -> None:
    first = store.put(f"{record_prefix}a.txt", b"first", "text/plain")
    with pytest.raises(RecordExists):
        store.put(f"{record_prefix}a.txt", b"second", "text/plain")
    assert store.get(first.key, first.generation, first.sha256) == b"first"


def test_get_detects_a_hash_mismatch(store: RecordStore, record_prefix: str) -> None:
    rec = store.put(f"{record_prefix}a.txt", b"hello", "text/plain")
    with pytest.raises(RecordIntegrityError, match="expected 0000"):
        store.get(rec.key, rec.generation, "0000")
