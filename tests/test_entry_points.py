"""The installed console scripts exist and behave as expected for this week."""

import os
import shutil
import signal
import subprocess
import threading

import pytest
from sqlalchemy import URL, create_engine, pool, text


def test_api_is_a_not_implemented_stub() -> None:
    path = shutil.which("jhunt-api")
    assert path is not None, "jhunt-api is not installed; run `uv sync`"

    result = subprocess.run([path], capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    assert result.stderr.strip() == "jhunt-api: not implemented"


def test_worker_starts_and_stops_cleanly_on_sigterm(
    gcs_test_bucket: str, migrated_db_url: URL
) -> None:
    path = shutil.which("jhunt-worker")
    assert path is not None, "jhunt-worker is not installed; run `uv sync`"

    # The worker wires Capture and Scout to the records and staging buckets at
    # startup: in tests both are the CI bucket, never the real ones. Its
    # database is a scratch one, never the shared one.
    env = os.environ | {
        "POSTGRES_DB": str(migrated_db_url.database),
        "GCS_RECORDS_BUCKET": gcs_test_bucket,
        "GCS_STAGING_BUCKET": gcs_test_bucket,
    }
    proc = subprocess.Popen([path], stderr=subprocess.PIPE, text=True, env=env)
    started = threading.Event()

    def watch() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if "jhunt-worker started" in line:
                started.set()

    threading.Thread(target=watch, daemon=True).start()
    try:
        if not started.wait(timeout=30):
            pytest.fail("jhunt-worker did not report that it started")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # It ran on the test's own database: at startup it recorded the graph
    # fingerprints there. Never the shared one, whose queue may hold real runs.
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        recorded = conn.execute(text("SELECT count(*) FROM graph_versions")).scalar_one()
    engine.dispose()
    assert recorded > 0
