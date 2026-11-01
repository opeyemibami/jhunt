"""The installed console scripts exist and behave as expected for this week."""

import os
import shutil
import signal
import socket
import subprocess
import threading

import httpx
import pytest
from sqlalchemy import URL, create_engine, pool, text


def test_api_serves_the_digest_on_127_0_0_1_only(
    gcs_test_bucket: str, migrated_db_url: URL
) -> None:
    path = shutil.which("jhunt-api")
    assert path is not None, "jhunt-api is not installed; run `uv sync`"
    with socket.socket() as probe:  # a free port for this test
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ | {
        "POSTGRES_DB": str(migrated_db_url.database),
        "GCS_RECORDS_BUCKET": gcs_test_bucket,
        "GCS_STAGING_BUCKET": gcs_test_bucket,
        "JHUNT_API_PORT": str(port),
    }
    proc = subprocess.Popen([path], stderr=subprocess.PIPE, text=True, env=env)
    listening = threading.Event()
    lines: list[str] = []

    def watch() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.append(line)
            if "Uvicorn running on" in line:
                listening.set()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        if not listening.wait(timeout=30):
            pytest.fail("jhunt-api did not start: " + "".join(lines))
        assert f"Uvicorn running on http://127.0.0.1:{port}" in "".join(lines)
        response = httpx.get(f"http://127.0.0.1:{port}/", timeout=10)
        assert response.status_code == 200
        assert "<h1>Scout digest</h1>" in response.text
        proc.send_signal(signal.SIGTERM)
        # uvicorn shuts down cleanly, then re-raises the signal: the exit
        # status is SIGTERM's, and the log says it finished.
        assert proc.wait(timeout=30) == -signal.SIGTERM
        watcher.join(timeout=5)
        assert "Finished server process" in "".join(lines)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


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
