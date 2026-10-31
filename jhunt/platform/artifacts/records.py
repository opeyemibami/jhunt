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


class RecordStore:
    def __init__(self, bucket_name: str, client: storage.Client | None = None) -> None:
        # The client uses Application Default Credentials: jhunt-app,
        # impersonated locally; Workload Identity in CI; never a key file.
        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def put(self, key: str, data: bytes, content_type: str) -> StoredRecord:
        """Write a new record. Raises RecordExists if the key is taken."""
        sha256 = hashlib.sha256(data).hexdigest()
        blob = self._bucket.blob(key)
        blob.metadata = {"sha256": sha256}
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
