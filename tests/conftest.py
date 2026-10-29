"""Shared fixtures. Database tests use the local Postgres from docker-compose.yml."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, pool, text

from jhunt.platform.config import Settings, get_settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Live tests spend real money and need real keys: opt in explicitly.
    if os.environ.get("RUN_LIVE_MODEL_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live provider call; set RUN_LIVE_MODEL_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


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


@pytest.fixture
def migrated_db_url(scratch_db_url: URL) -> URL:
    """A scratch database upgraded to the latest migration."""
    cfg = Config(toml_file=Path(__file__).resolve().parent.parent / "pyproject.toml")
    cfg.attributes["database_url"] = scratch_db_url
    command.upgrade(cfg, "head")
    return scratch_db_url
