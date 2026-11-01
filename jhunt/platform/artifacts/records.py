"""Records: objects that must never be lost or altered, such as job description
snapshots (ADR 0002).

Every record is written once (create-only) with its own retention: Unlocked
mode, held for three years from the write. Both are fixed here, with no
parameter, so the app can never choose a Locked mode or another date (ADR 0002,
residual risk). Reads check the sha256 recorded at write time.

The app identity (jhunt-app) can create and read records, and set retention on
write, nothing else. IAM, not this code, is what stops deletes and overwrites;
tests/guarantees/test_snapshot_immutable.py proves it.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

# Unlocked: the owner can still remove an object's retention to delete one
# application's records. Locked would make that impossible until the date.
RETENTION_MODE = "Unlocked"
# Three years, counting one leap day.
RETENTION_PERIOD = dt.timedelta(days=3 * 365 + 1)


class RecordExists(Exception):
    """A record already exists under this key. Records are never replaced."""


class RecordIntegrityError(Exception):
    """A record's bytes don't match the sha256 recorded when it was written."""


@dataclass(frozen=True)
class StoredRecord:
    uri: str  # gs://bucket/key
    key: str
    generation: int  # the exact version of the object; reads use it
    sha256: str
    size: int


class _Store:
    """Create-only objects with a sha256, shared by RecordStore and StagingStore.

    Not a public base: the two are deliberately separate types, so code that
    needs records (with retention) can't be handed a staging store.
    """

    def __init__(self, bucket_name: str, client: storage.Client | None = None) -> None:
        # The client uses Application Default Credentials: jhunt-app,
        # impersonated locally; Workload Identity in CI; never a key file.
        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def put(self, key: str, data: bytes, content_type: str) -> StoredRecord:
        raise NotImplementedError

    def _create(self, key: str, data: bytes, content_type: str, *, retain: bool) -> StoredRecord:
        sha256 = hashlib.sha256(data).hexdigest()
        blob = self._bucket.blob(key)
        blob.metadata = {"sha256": sha256}
        if retain:
            blob.retention.mode = RETENTION_MODE
            blob.retention.retain_until_time = dt.datetime.now(dt.UTC) + RETENTION_PERIOD
        try:
            # if_generation_match=0: create only. The upload fails if any live
            # object has this name, so a record is never silently replaced.
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except PreconditionFailed:
            raise RecordExists(f"gs://{self._bucket.name}/{key}") from None
        if blob.generation is None:  # GCS always returns it; never write a row without it
            raise RuntimeError(f"upload of {key} returned no generation")
        return StoredRecord(
            uri=f"gs://{self._bucket.name}/{key}",
            key=key,
            generation=blob.generation,
            sha256=sha256,
            size=len(data),
        )

    def put_or_adopt(self, key: str, data: bytes, content_type: str) -> tuple[StoredRecord, bytes]:
        """Write a new record, or adopt the one already stored under this key.

        Returns the stored record and its bytes. When the key already exists
        (an earlier attempt of the same step, or another worker), the existing
        object wins: its generation is read from storage and its sha256 is
        computed from its downloaded bytes, never from metadata and never from
        the bytes just offered, which may differ (ADR 0007).
        """
        try:
            return self.put(key, data, content_type), data
        except RecordExists:
            adopted = self.existing(key)
            if adopted is None:  # it existed a moment ago; records are never deleted by the app
                raise RuntimeError(
                    f"gs://{self._bucket.name}/{key} exists but could not be read"
                ) from None
            return adopted

    def existing(self, key: str) -> tuple[StoredRecord, bytes] | None:
        """The live record under this key and its bytes, or None if there is none.

        Same rule as adopting: the generation comes from storage and the
        sha256 from the downloaded bytes, never from metadata.
        """
        blob = self._bucket.get_blob(key)  # the live generation, or None
        if blob is None or blob.generation is None:
            return None
        data: bytes = self._bucket.blob(key, generation=blob.generation).download_as_bytes()
        record = StoredRecord(
            uri=f"gs://{self._bucket.name}/{key}",
            key=key,
            generation=blob.generation,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        return record, data

    def get(self, key: str, generation: int, expected_sha256: str) -> bytes:
        """Read exactly this generation of a record and check its sha256."""
        data: bytes = self._bucket.blob(key, generation=generation).download_as_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise RecordIntegrityError(
                f"gs://{self._bucket.name}/{key}#{generation}: sha256 {actual}, "
                f"expected {expected_sha256}"
            )
        return data


class RecordStore(_Store):
    """Records: never lost or altered (ADR 0002)."""

    def put(self, key: str, data: bytes, content_type: str) -> StoredRecord:
        """Write a new record. Raises RecordExists if the key is taken.

        Always with the fixed Unlocked 3-year retention: there is no way to
        ask for less, or for Locked mode.
        """
        return self._create(key, data, content_type, retain=True)


class StagingStore(_Store):
    """Scratch objects in the staging bucket, such as board payloads (ADR 0010).

    Create-only and sha256-checked like records, but with no retention: the
    staging bucket's 30-day lifecycle removes them.
    """

    def put(self, key: str, data: bytes, content_type: str) -> StoredRecord:
        """Write a new object. Raises RecordExists if the key is taken."""
        return self._create(key, data, content_type, retain=False)
