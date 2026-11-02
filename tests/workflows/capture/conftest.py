"""Fixtures for Capture: the record store on the real CI bucket and an HTTP
client. The local web server (`site`) comes from tests/runtime/local_site.py."""

from collections.abc import Iterator

import httpx
import pytest
from google.cloud import storage

from jhunt.platform.artifacts import RecordStore


@pytest.fixture
def store(gcs_test_bucket: str, gcs_client: storage.Client) -> RecordStore:
    return RecordStore(gcs_test_bucket, gcs_client)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10) as client:
        yield client
