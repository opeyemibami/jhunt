"""jhunt-scout queues one Scout run a day over the boards in sources.toml."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, pool, text

from jhunt.app import scout as scout_cli
from jhunt.workflows import scout

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def scratch_env(
    monkeypatch: pytest.MonkeyPatch, migrated_db_url: URL, gcs_test_bucket: str
) -> Iterator[URL]:
    # Point the command at the scratch database and the CI bucket, never the
    # dev database or the real buckets.
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCS_RECORDS_BUCKET", gcs_test_bucket)
    monkeypatch.setenv("GCS_STAGING_BUCKET", gcs_test_bucket)
    monkeypatch.setenv("JHUNT_CONFIG_DIR", str(REPO_CONFIG))
    monkeypatch.setattr(scout_cli, "today", lambda: "2026-11-01")
    scout_cli.get_settings.cache_clear()
    yield migrated_db_url
    scout_cli.get_settings.cache_clear()


def test_queues_todays_run_once_with_every_source(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["jhunt-scout"])
    thread_id = scout.thread_id_for("2026-11-01")

    scout_cli.main()
    assert capsys.readouterr().out.strip() == f"queued: thread {thread_id} (13 boards)"
    scout_cli.main()
    assert capsys.readouterr().out.strip() == (
        f"already ran today: thread {thread_id}; Scout runs once a day"
    )

    engine = create_engine(scratch_env, poolclass=pool.NullPool)
    with engine.connect() as conn:
        [payload] = conn.execute(
            text("SELECT payload FROM runs WHERE thread_id = :t"), {"t": thread_id}
        ).scalars()
    engine.dispose()
    assert payload["day"] == "2026-11-01"
    assert len(payload["sources"]) == 13
    assert payload["northern_ireland_in_scope"] is False  # from config/scope.toml


def test_a_bad_config_stops_before_queueing(
    scratch_env: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JHUNT_CONFIG_DIR", str(tmp_path))
    scout_cli.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["jhunt-scout"])
    with pytest.raises(SystemExit, match="jhunt-scout: sources file missing"):
        scout_cli.main()
