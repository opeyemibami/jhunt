"""Shared fixtures. Database tests use the local Postgres from docker-compose.yml."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import URL, create_engine, pool, text

from jhunt.platform.config import Settings, get_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def scratch_db_url(settings: Settings) -> Iterator[URL]:
    """A fresh, empty database for one test, dropped afterwards.

    Tests that migrate up and down must never touch the developer's database.
    """
    name = f"jhunt_test_{uuid.uuid4().hex[:12]}"
    # CREATE/DROP DATABASE can't run inside a transaction.
    admin = create_engine(
        settings.database_url(), isolation_level="AUTOCOMMIT", poolclass=pool.NullPool
    )
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield settings.database_url(name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
