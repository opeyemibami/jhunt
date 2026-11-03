"""Object storage: records and staging on GCS (ADR 0002). The only code that
touches a storage SDK.
"""

from jhunt.platform.artifacts.records import (
    ObjectMissing,
    RecordExists,
    RecordIntegrityError,
    RecordStore,
    StagingStore,
    StoredRecord,
)

__all__ = [
    "ObjectMissing",
    "RecordExists",
    "RecordIntegrityError",
    "RecordStore",
    "StagingStore",
    "StoredRecord",
]
