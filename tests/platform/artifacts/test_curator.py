"""The owner's delete removes every generation under a prefix, live and noncurrent.

Owner-only: it needs the curator role, so it runs as the owner's gcloud login
and only with RUN_OWNER_TESTS=1. Records are written as the app, as in real use.
"""

import os

import pytest

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.artifacts.curator import (
    delete_all_generations,
    list_generations,
    owner_client,
)

pytestmark = pytest.mark.owner


def test_delete_all_generations(gcs_test_bucket: str, record_prefix: str) -> None:
    owner = owner_client(os.environ["GCP_PROJECT"])
    app = RecordStore(gcs_test_bucket)
    key = f"{record_prefix}snapshot.html"

    # Two generations of one key: the first made noncurrent by an owner delete,
    # the second written again by the app. Both carry the 3-year retention.
    first = app.put(key, b"v1", "text/html")
    stale = owner.bucket(gcs_test_bucket).blob(key, generation=first.generation)
    stale.reload()
    stale.retention.mode = None
    stale.retention.retain_until_time = None
    stale.patch(override_unlocked_retention=True)
    owner.bucket(gcs_test_bucket).blob(key).delete()  # no generation: becomes noncurrent
    app.put(key, b"v2", "text/html")

    before = list_generations(gcs_test_bucket, record_prefix, owner)
    assert sorted(g.live for g in before) == [False, True]

    deleted = delete_all_generations(gcs_test_bucket, record_prefix, owner)

    assert len(deleted) == 2
    assert list_generations(gcs_test_bucket, record_prefix, owner) == []
