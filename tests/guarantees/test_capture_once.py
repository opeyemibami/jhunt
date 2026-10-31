"""Guarantee: a posting is captured once.

Two captures of the same posting, run in parallel by two real worker
processes, make one application, one snapshot row and one event. Two paths:

- the same thread: the second start is refused (ThreadExists);
- two different threads for the same posting, which bypasses that check: the
  workers race on the insert (a rendezvous makes them both pass the "exists
  yet?" check first), and the unique dedupe_key keeps it to one (ADR 0007).
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import capture
from tests.guarantees.workers import stop_worker, wait_until
from tests.runtime import capture_worker
from tests.runtime.local_site import Site

REPO_ROOT = Path(__file__).resolve().parents[2]
ABOUT = (
    "Own the job queue and the storage guarantees behind a durable workflow platform. "
    "You will work in Python and Postgres, close to production, with real autonomy. "
) * 4
PAGE = f"<html><body><h1>Backend Engineer</h1><p>Dublin</p><p>{ABOUT}</p></body></html>".encode()


@pytest.fixture
def capture_db(migrated_db_url: URL) -> Iterator[Engine]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        conn.execute(text(capture_worker.RENDEZVOUS_DDL))
    yield engine
    with engine.begin() as conn:
        conn.execute(text(capture_worker.RENDEZVOUS_DROP))
    engine.dispose()


def start_worker(db: URL, bucket: str, prefix: str) -> subprocess.Popen[str]:
    env = os.environ | {
        "POSTGRES_DB": str(db.database),
        "GCS_TEST_BUCKET": bucket,
        "TEST_RECORD_PREFIX": prefix,
        "RENDEZVOUS": "2",
        "LANGGRAPH_STRICT_MSGPACK": "true",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.runtime.capture_worker"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def run_statuses(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return sorted(conn.execute(text("SELECT status FROM runs")).scalars())


def test_two_parallel_captures_make_one_application(
    capture_db: Engine,
    migrated_db_url: URL,
    site: Site,
    gcs_test_bucket: str,
    record_prefix: str,
) -> None:
    site.pages["jobs/7"] = (200, PAGE)
    url = site.url("jobs/7")
    key = capture.dedupe_key(url, "Acme", "Backend Engineer")
    graph_input = {"url": url, "company": "Acme", "title": "Backend Engineer", "dedupe_key": key}
    registry = capture_worker.registry_for(capture_db, gcs_test_bucket, record_prefix)

    # Path 1: the same thread. The second start is refused outright.
    enqueue_start(capture_db, registry, capture.thread_id_for(key), capture.WORKFLOW, graph_input)
    with pytest.raises(ThreadExists):
        enqueue_start(
            capture_db, registry, capture.thread_id_for(key), capture.WORKFLOW, graph_input
        )

    # Path 2: a second thread for the same posting, so only the database can stop it.
    other_thread = f"{capture.thread_id_for(key)}-{uuid.uuid4().hex[:6]}"
    enqueue_start(capture_db, registry, other_thread, capture.WORKFLOW, graph_input)

    workers = [start_worker(migrated_db_url, gcs_test_bucket, record_prefix) for _ in range(2)]
    try:
        wait_until(
            lambda: run_statuses(capture_db) == ["succeeded", "succeeded"],
            "both captures finish",
            tuple(workers),
        )
    finally:
        for worker in workers:
            stop_worker(worker)

    assert count(capture_db, capture_worker.RENDEZVOUS_TABLE) == 2  # they really raced
    assert count(capture_db, "applications") == 1
    assert count(capture_db, "jd_snapshots") == 1  # the loser's row was rolled back
    assert count(capture_db, "events") == 1
    with capture_db.connect() as conn:
        refs = conn.execute(text("SELECT result FROM tool_calls")).scalars().all()
    assert len(refs) == 2  # one tool call per thread...
    assert refs[0] == refs[1]  # ...both pointing at the one snapshot that was kept
