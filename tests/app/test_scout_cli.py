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
        f"already ran today: thread {thread_id} (queued); Scout runs once a day"
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
    assert payload["targets"] == scout.load_targets(REPO_CONFIG).as_input()


def jhunt_scout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> str:
    monkeypatch.setattr(sys, "argv", ["jhunt-scout", *args])
    scout_cli.main()
    return capsys.readouterr().out.strip()


def close(db_url: URL, thread_id: str, status: str) -> None:
    """Close a thread's run as the worker would."""
    engine = create_engine(db_url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = :s, error = :e WHERE thread_id = :t"),
            {"s": status, "e": "storage down" if status == "failed" else None, "t": thread_id},
        )
    engine.dispose()


def test_retry_starts_another_run_only_after_a_failure(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    day = "2026-11-01"
    assert jhunt_scout(monkeypatch, capsys, "--retry") == (
        "nothing to retry: no Scout run today; run jhunt-scout"
    )
    assert jhunt_scout(monkeypatch, capsys) == f"queued: thread scout-{day} (13 boards)"
    assert jhunt_scout(monkeypatch, capsys, "--retry") == (
        f"not retrying: today's run is queued (thread scout-{day})"
    )

    close(scratch_env, f"scout-{day}", "failed")
    assert jhunt_scout(monkeypatch, capsys) == (
        f"today's run failed: thread scout-{day}; jhunt-scout --retry starts another"
    )
    assert (
        jhunt_scout(monkeypatch, capsys, "--retry") == f"queued: thread scout-{day}-2 (13 boards)"
    )
    assert jhunt_scout(monkeypatch, capsys, "--retry") == (
        f"not retrying: today's run is queued (thread scout-{day}-2)"
    )

    close(scratch_env, f"scout-{day}-2", "failed")
    assert (
        jhunt_scout(monkeypatch, capsys, "--retry") == f"queued: thread scout-{day}-3 (13 boards)"
    )

    close(scratch_env, f"scout-{day}-3", "succeeded")
    assert jhunt_scout(monkeypatch, capsys, "--retry") == (
        f"not retrying: today's run succeeded (thread scout-{day}-3)"
    )
    assert jhunt_scout(monkeypatch, capsys) == (
        f"already ran today: thread scout-{day}-3 (succeeded); Scout runs once a day"
    )


def test_retry_counts_only_todays_threads(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Yesterday's failed run is not today's: --retry has nothing to retry.
    monkeypatch.setattr(scout_cli, "today", lambda: "2026-10-31")
    jhunt_scout(monkeypatch, capsys)
    close(scratch_env, "scout-2026-10-31", "failed")
    monkeypatch.setattr(scout_cli, "today", lambda: "2026-11-01")
    assert jhunt_scout(monkeypatch, capsys, "--retry") == (
        "nothing to retry: no Scout run today; run jhunt-scout"
    )


def test_rescore_runs_again_only_after_a_success(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    day = "2026-11-01"
    assert jhunt_scout(monkeypatch, capsys, "--rescore") == (
        "nothing to rescore: no Scout run today; run jhunt-scout"
    )
    jhunt_scout(monkeypatch, capsys)
    assert jhunt_scout(monkeypatch, capsys, "--rescore") == (
        f"not rescoring: today's run is queued (thread scout-{day})"
    )
    close(scratch_env, f"scout-{day}", "failed")
    assert jhunt_scout(monkeypatch, capsys, "--rescore") == (
        f"not rescoring: today's run failed (thread scout-{day}); use --retry"
    )
    jhunt_scout(monkeypatch, capsys, "--retry")
    close(scratch_env, f"scout-{day}-2", "succeeded")
    # Same day: the board lists already in staging are scored again, not fetched.
    assert jhunt_scout(monkeypatch, capsys, "--rescore") == (
        f"queued: thread scout-{day}-3 (13 boards)"
    )
    close(scratch_env, f"scout-{day}-3", "succeeded")
    assert jhunt_scout(monkeypatch, capsys, "--rescore") == (
        f"queued: thread scout-{day}-4 (13 boards)"
    )


def test_retry_and_rescore_cannot_be_combined(
    scratch_env: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["jhunt-scout", "--retry", "--rescore"])
    with pytest.raises(SystemExit):
        scout_cli.main()


def test_a_bad_config_stops_before_queueing(
    scratch_env: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JHUNT_CONFIG_DIR", str(tmp_path))
    scout_cli.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["jhunt-scout"])
    with pytest.raises(SystemExit, match="jhunt-scout: sources file missing"):
        scout_cli.main()
