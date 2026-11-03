"""jhunt-scout --wait against a real jhunt-worker process: it exits with the
summary when the run succeeds, and with the run's error when it fails."""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, pool, text

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
# Well under jhunt-scout's own five-minute wait: a hang fails the test here.
TIMEOUT = 60


def config_dir(tmp_path: Path, sources: str) -> Path:
    """The repo's scope, targets and claim levels, with the test's own
    sources: never the real boards."""
    # The worker's wiring also reads the claim level words (ADR 0015).
    for name in ("scope.toml", "targets.toml", "claim_levels.toml"):
        shutil.copy(REPO_CONFIG / name, tmp_path / name)
    (tmp_path / "sources.toml").write_text(sources)
    return tmp_path


@pytest.fixture
def worker_env(migrated_db_url: URL, gcs_test_bucket: str) -> dict[str, str]:
    return os.environ | {
        "POSTGRES_DB": str(migrated_db_url.database),
        "GCS_RECORDS_BUCKET": gcs_test_bucket,
        "GCS_STAGING_BUCKET": gcs_test_bucket,
    }


def start_worker(env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    path = shutil.which("jhunt-worker")
    assert path is not None, "jhunt-worker is not installed; run `uv sync`"
    proc = subprocess.Popen([path], env=env, stderr=subprocess.DEVNULL, text=True)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def scout_wait(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = shutil.which("jhunt-scout")
    assert path is not None, "jhunt-scout is not installed; run `uv sync`"
    return subprocess.run(
        [path, "--wait"], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


def test_wait_prints_the_summary_when_the_run_succeeds(
    worker_env: dict[str, str], tmp_path: Path
) -> None:
    # No boards: the run fetches nothing and succeeds without the network.
    env = worker_env | {"JHUNT_CONFIG_DIR": str(config_dir(tmp_path, "sources = []\n"))}
    for _ in start_worker(env):
        result = scout_wait(env)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("queued: thread scout-")
    assert lines[1].endswith(": 0 boards, 0 failed")
    assert lines[2:] == [
        "postings: 0 first seen in this run, 0 seen again, 0 in scope",
        "removed: 0 unseen for 30 days",
    ]


def test_wait_exits_with_the_error_when_the_run_fails(
    worker_env: dict[str, str], migrated_db_url: URL, tmp_path: Path
) -> None:
    # fetch_postings set to level 0 in the scratch database: the gateway
    # refuses fetch_board before it runs, so nothing is requested from the
    # board, and the worker fails the run with ToolRefused.
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO autonomy_settings (action, level, changed_by)"
                " VALUES ('fetch_postings', 0, 'test')"
            )
        )
    engine.dispose()
    sources = '[[sources]]\nprovider = "ashby"\nboard = "acme"\ncompany = "Acme"\n'
    env = worker_env | {"JHUNT_CONFIG_DIR": str(config_dir(tmp_path, sources))}
    for _ in start_worker(env):
        result = scout_wait(env)
    assert result.returncode == 1
    assert result.stdout.strip().startswith("queued: thread scout-")
    assert result.stderr.strip().startswith("jhunt-scout: the run failed: ToolRefused: ")
