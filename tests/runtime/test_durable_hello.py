"""A graph survives a real SIGKILL mid-step and resumes in a new process.

Records which nodes actually ran, in which process, so ADR 0004 can state what
LangGraph re-runs after a crash. AGENTS.md assumes it re-runs the whole
interrupted step and nothing before it.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, pool, text

from tests.runtime import hello_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 60.0


@pytest.fixture
def probe_db(migrated_db_url: URL) -> Iterator[URL]:
    """The migrated scratch database plus the probe table this test owns."""
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE {hello_graph.PROBE_TABLE} ("
                " id bigserial PRIMARY KEY, thread_id text NOT NULL, node text NOT NULL,"
                " pid integer NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp())"
            )
        )
    try:
        yield migrated_db_url
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {hello_graph.PROBE_TABLE}"))
        engine.dispose()


def start(mode: str, thread_id: str, db: URL, hang_in: str | None = None) -> subprocess.Popen[str]:
    env = os.environ | {"POSTGRES_DB": str(db.database), "LANGGRAPH_STRICT_MSGPACK": "true"}
    env.pop("HELLO_HANG_IN", None)
    if hang_in is not None:
        env["HELLO_HANG_IN"] = hang_in
    return subprocess.Popen(
        [sys.executable, "-m", "tests.runtime.hello_graph", mode, thread_id],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def probe_rows(db: URL, thread_id: str) -> list[tuple[str, int]]:
    engine = create_engine(db, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT node, pid FROM {hello_graph.PROBE_TABLE}"
                    " WHERE thread_id = :t ORDER BY id"
                ),
                {"t": thread_id},
            )
            return [(r.node, r.pid) for r in rows]
    finally:
        engine.dispose()


def wait_for_probe(db: URL, thread_id: str, node: str, proc: subprocess.Popen[str]) -> None:
    """Block until `node` has recorded its start. Polls a condition, not a fixed sleep."""
    deadline = time.monotonic() + TIMEOUT
    while (node, proc.pid) not in probe_rows(db, thread_id):
        if proc.poll() is not None:
            pytest.fail(f"worker exited early ({proc.returncode}): {proc.stderr.read()}")  # type: ignore[union-attr]
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"{node} never started")
        time.sleep(0.05)


def checkpoint_history(db: URL, thread_id: str) -> list[dict[str, object]]:
    info = db.set(drivername="postgresql").render_as_string(hide_password=False)
    conn, saver = hello_graph.open_checkpointer(info)
    with conn:
        graph = hello_graph.build(saver)
        config = {"configurable": {"thread_id": thread_id}}
        return [
            {"step": s.metadata["step"], "trail": s.values.get("trail"), "next": s.next}  # type: ignore[index]
            for s in reversed(list(graph.get_state_history(config)))  # type: ignore[arg-type]
        ]


def test_sigkill_mid_step_then_resume(probe_db: URL) -> None:
    thread_id = f"hello-{uuid.uuid4().hex[:8]}"

    first = start("run", thread_id, probe_db, hang_in="step2")
    try:
        wait_for_probe(probe_db, thread_id, "step2", first)
        os.kill(first.pid, signal.SIGKILL)
        assert first.wait(timeout=TIMEOUT) == -signal.SIGKILL
    finally:
        if first.poll() is None:
            first.kill()

    after_kill = checkpoint_history(probe_db, thread_id)

    second = start("resume", thread_id, probe_db)
    _, stderr = second.communicate(timeout=TIMEOUT)
    assert second.returncode == 0, stderr

    rows = probe_rows(probe_db, thread_id)
    history = checkpoint_history(probe_db, thread_id)
    print(f"\nlanggraph probe rows: {rows}\nafter kill: {after_kill}\nfinal: {history}")

    # step1 ran once, in the killed process. step2 started there, was killed,
    # and ran again from the start in the new process. step3 ran once.
    assert rows == [
        ("step1", first.pid),
        ("step2", first.pid),
        ("step2", second.pid),
        ("step3", second.pid),
    ]
    # With durability="sync" the checkpoint after step1 was on disk before the kill.
    assert after_kill[-1]["trail"] == ["step1"]
    assert after_kill[-1]["next"] == ("step2",)
    assert history[-1]["trail"] == ["step1", "step2", "step3"]
    assert history[-1]["next"] == ()
