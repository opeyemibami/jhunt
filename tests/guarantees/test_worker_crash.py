"""Guarantee: a run survives a worker crash.

Kill a real worker process (SIGKILL) in the middle of a step; another worker
reclaims the run once the lease expires, resumes from the last checkpoint, and
the run completes. Only the interrupted step runs again (ADR 0004).

"Side effects once" is added with the tool gateway in week 3 (docs/backlog.md).
"""

import os
import signal
import uuid

from sqlalchemy import URL, create_engine, pool

from jhunt.platform.runtime import queue
from tests.guarantees.workers import (
    checkpoint_trails,
    probes,
    run_row,
    start_worker,
    stop_worker,
    wait_until,
)
from tests.runtime.worker_fixture import WORKFLOW


def test_run_survives_a_worker_crash(crash_db: URL) -> None:
    engine = create_engine(crash_db, poolclass=pool.NullPool)
    thread_id = f"crash-{uuid.uuid4().hex[:8]}"
    run_id = queue.enqueue(engine, thread_id, WORKFLOW, 1, "start", {"trail": []})

    a = start_worker(crash_db, hang_in_step2=True)
    try:
        wait_until(lambda: ("step2", a.pid) in probes(engine, thread_id), "A starts step2", (a,))
        os.kill(a.pid, signal.SIGKILL)
        assert a.wait(timeout=10) == -signal.SIGKILL
    finally:
        if a.poll() is None:
            a.kill()

    b = start_worker(crash_db)
    try:
        # B can only claim the run once A's lease has expired (2 s here).
        wait_until(lambda: run_row(engine, run_id)["status"] == "succeeded", "B completes", (b,))
    finally:
        stop_worker(b)

    # step1 ran once (A). step2 started in A, was killed, and ran again in B.
    # step3 ran once (B).
    assert probes(engine, thread_id) == [
        ("step1", a.pid),
        ("step2", a.pid),
        ("step2", b.pid),
        ("step3", b.pid),
    ]
    run = run_row(engine, run_id)
    assert (run["attempts"], run["error"], run["lease_owner"]) == (2, None, None)
    final_trail = checkpoint_trails(crash_db, thread_id)[-1]
    assert final_trail == [f"step1@{a.pid}", f"step2@{b.pid}", f"step3@{b.pid}"]
    engine.dispose()
