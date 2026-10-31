"""Helpers for the guarantee tests that start, kill and stop real worker processes."""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import URL, Engine, text

from tests.runtime import worker_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]
LEASE_SECONDS = 2.0
HEARTBEAT_SECONDS = 0.5
TIMEOUT = 30.0


def start_worker(db: URL, hang_in_step2: bool = False) -> subprocess.Popen[str]:
    env = os.environ | {
        "POSTGRES_DB": str(db.database),
        "LANGGRAPH_STRICT_MSGPACK": "true",
        "TEST_LEASE_SECONDS": str(LEASE_SECONDS),
        "TEST_HEARTBEAT_SECONDS": str(HEARTBEAT_SECONDS),
    }
    env.pop("HANG_IN_STEP2", None)
    if hang_in_step2:
        env["HANG_IN_STEP2"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "tests.runtime.worker_fixture"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_worker(proc: subprocess.Popen[str]) -> None:
    """SIGTERM an idle worker; kill it if it doesn't stop."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def wait_until(
    condition: Callable[[], bool], what: str, procs: tuple[subprocess.Popen[str], ...] = ()
) -> None:
    """Poll a condition (not a fixed sleep) until it holds, failing early if a worker dies."""
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        for proc in procs:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                pytest.fail(f"worker {proc.pid} exited ({proc.returncode}) before {what}:\n{err}")
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.05)


def probes(engine: Engine, thread_id: str) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT node, pid FROM {worker_fixture.PROBE_TABLE}"
                " WHERE thread_id = :t ORDER BY id"
            ),
            {"t": thread_id},
        )
        return [(r.node, r.pid) for r in rows]


def run_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id})
        return dict(row.mappings().one())


def checkpoint_trails(db: URL, thread_id: str) -> list[list[str]]:
    """The trail stored in every checkpoint of the thread, oldest first."""
    info = db.set(drivername="postgresql").render_as_string(hide_password=False)
    with Connection.connect(info, autocommit=True, row_factory=dict_row) as conn:
        graph = worker_fixture.build(PostgresSaver(conn))
        config = {"configurable": {"thread_id": thread_id}}
        history = list(graph.get_state_history(config))  # type: ignore[arg-type]
    return [list(s.values.get("trail", [])) for s in reversed(history)]
