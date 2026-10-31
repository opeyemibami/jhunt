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
from tests.runtime.lease_watch import LeaseWatch


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Live tests spend real money and need real keys; owner tests need the
    # owner's gcloud login and the curator role. Both are opt-in.
    opt_in = {
        "live": ("RUN_LIVE_MODEL_TESTS", "live provider call"),
        "owner": ("RUN_OWNER_TESTS", "needs the owner's gcloud login"),
    }
    for marker, (variable, why) in opt_in.items():
        if os.environ.get(variable) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{why}; set {variable}=1 to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def gcs_test_bucket() -> str:
    """The real CI bucket, set up like records (ADR 0002). Never a fake store."""
    name = os.environ.get("GCS_TEST_BUCKET")
    if not name:
        pytest.fail("GCS_TEST_BUCKET is not set: storage tests need the real CI bucket")
    return name


@pytest.fixture
def record_prefix() -> str:
    """A fresh prefix per test. The owner clears old ones with jhunt-records-delete."""
    return f"tests/{uuid.uuid4().hex}/"


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


@pytest.fixture
def lease_watch() -> Iterator[LeaseWatch]:
    """The lease-lost handler for workers run inside the test process.

    Fails the test at teardown if any lease was lost, naming the run and why.
    """
    watch = LeaseWatch()
    yield watch
    watch.check()
