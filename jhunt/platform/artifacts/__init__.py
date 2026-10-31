"""Object storage: records and staging on GCS (ADR 0002). The only code that
touches a storage SDK.
"""

from jhunt.platform.artifacts.records import (
    RecordExists,
    RecordIntegrityError,
    RecordStore,
    StoredRecord,
)

__all__ = ["RecordExists", "RecordIntegrityError", "RecordStore", "StoredRecord"]
