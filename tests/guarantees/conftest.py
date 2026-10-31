"""Fixtures for the guarantee tests."""

from collections.abc import Iterator

import pytest
from sqlalchemy import URL, create_engine, pool, text

from tests.runtime import worker_fixture


@pytest.fixture
def crash_db(migrated_db_url: URL) -> Iterator[URL]:
    """A migrated scratch database plus the probe tables the crash tests own."""
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        for ddl in worker_fixture.PROBE_DDL:
            conn.execute(text(ddl))
    try:
        yield migrated_db_url
    finally:
        with engine.begin() as conn:
            for ddl in worker_fixture.PROBE_DROP:
                conn.execute(text(ddl))
        engine.dispose()
