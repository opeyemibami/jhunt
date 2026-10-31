"""Guarantee: a worker that lost its lease stops at once and writes nothing more.

Freeze worker A mid-step (SIGSTOP) until its lease expires and worker B has
taken the run over and completed it. Then wake A (SIGCONT). A's next heartbeat
finds the lease gone and the process exits immediately: it must not write a
checkpoint for the thread or touch the run row (ADR 0005).
"""

import os
import signal
import subprocess
import uuid

import pytest
from sqlalchemy import URL, create_engine, pool

from jhunt.platform.runtime import queue
from jhunt.platform.runtime.worker import LOST_LEASE_EXIT
from tests.guarantees.workers import (
    checkpoint_trails,
    probes,
    run_row,
    start_worker,
    stop_worker,
    wait_until,
)
from tests.runtime.worker_fixture import WORKFLOW


def test_worker_that_lost_its_lease_exits_without_writing(crash_db: URL) -> None:
    engine = create_engine(crash_db, poolclass=pool.NullPool)
    thread_id = f"fence-{uuid.uuid4().hex[:8]}"
    run_id = queue.enqueue(engine, thread_id, WORKFLOW, 1, "start", {"trail": []})

    a = start_worker(crash_db, hang_in_step2=True)
    try:
        wait_until(lambda: ("step2", a.pid) in probes(engine, thread_id), "A starts step2", (a,))
        os.kill(a.pid, signal.SIGSTOP)  # A is frozen mid-step, holding its lease

        b = start_worker(crash_db)
        try:
            wait_until(
                lambda: run_row(engine, run_id)["status"] == "succeeded", "B completes", (b,)
            )
        finally:
            stop_worker(b)

        run_before = run_row(engine, run_id)
        trails_before = checkpoint_trails(crash_db, thread_id)

        os.kill(a.pid, signal.SIGCONT)  # A wakes up; its lease is long gone
        try:
            code = a.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("A kept running after losing its lease")
        assert code == LOST_LEASE_EXIT

        # A touched nothing after waking: the run row is exactly as B left it,
        # and no checkpoint was added or changed.
        assert run_row(engine, run_id) == run_before
        assert checkpoint_trails(crash_db, thread_id) == trails_before
        assert not any(f"step2@{a.pid}" in trail for trail in trails_before)
        assert (run_before["status"], run_before["attempts"]) == ("succeeded", 2)
    finally:
        if a.poll() is None:
            os.kill(a.pid, signal.SIGCONT)
            a.kill()
            a.wait()
        engine.dispose()
