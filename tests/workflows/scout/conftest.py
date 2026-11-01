"""Fixtures for Scout: the staging store on the real CI bucket, an HTTP client
and a database engine. The local web server (`site`) comes from
tests/runtime/local_site.py."""

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import URL, Engine, create_engine, pool

from jhunt.platform.artifacts import StagingStore


@pytest.fixture
def staging(gcs_test_bucket: str) -> StagingStore:
    return StagingStore(gcs_test_bucket)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10) as client:
        yield client


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()
