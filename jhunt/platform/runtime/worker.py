"""The worker loop: claim a run, run its graph, close the run (ADR 0005).

While a run is in progress, a heartbeat thread extends the lease. If the lease
can't be extended, another worker may already own the run, so this process
exits on the spot (self-fencing) instead of carrying on beside it.
"""

import logging
import os
import threading
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine

from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry

log = logging.getLogger(__name__)

# Extend the lease every 10 s; it lasts 30 s, so two heartbeats can be missed.
HEARTBEAT_SECONDS = 10.0
# Exit code when the lease is lost: 75 is EX_TEMPFAIL, "try again later".
LOST_LEASE_EXIT = 75


def run_worker(
    engine: Engine,
    conninfo: str,
    registry: Registry,
    *,
    lease_seconds: float = queue.LEASE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    idle_seconds: float = 1.0,
    stop: threading.Event | None = None,
) -> None:
    """Claim and process runs until `stop` is set. One run at a time."""
    stop = stop or threading.Event()
    while not stop.is_set():
        run = queue.claim(engine, lease_seconds)
        if run is None:
            stop.wait(idle_seconds)  # nothing to do; wakes early if stopped
            continue
        process_run(
            engine,
            conninfo,
            registry,
            run,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )


def process_run(
    engine: Engine,
    conninfo: str,
    registry: Registry,
    run: queue.Run,
    *,
    lease_seconds: float = queue.LEASE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> None:
    """Run one claimed run's graph and close the run."""
    builder = registry.get(run.workflow, run.version)
    if builder is None:
        # Old versions stay registered until their last thread closes, so this
        # means a bad deploy. Fail loudly rather than leave the run queued.
        error = f"not registered: workflow {run.workflow!r} version {run.version}"
        log.error("run %s: %s", run.id, error)
        queue.fail(engine, run, error)
        return

    done = threading.Event()
    beat = threading.Thread(
        target=_heartbeat,
        args=(engine, run, lease_seconds, heartbeat_seconds, done),
        daemon=True,
    )
    beat.start()
    try:
        # The checkpointer's connection settings AGENTS.md requires.
        with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
            graph = builder(PostgresSaver(conn))
            config: RunnableConfig = {"configurable": {"thread_id": run.thread_id}}
            # durability="sync": each checkpoint is saved before the next step
            # starts, so a crash loses at most the step in progress (ADR 0004).
            graph.invoke(graph_input(graph, config, run), config, durability="sync")
            waiting = bool(graph.get_state(config).interrupts)
    except Exception as exc:
        log.exception("run %s failed", run.id)
        _stop(beat, done)
        queue.fail(engine, run, f"{type(exc).__name__}: {exc}")
        return
    finally:
        _stop(beat, done)

    status = "interrupted" if waiting else "succeeded"
    if not queue.finish(engine, run, status):
        # The lease was lost between the last heartbeat and now. The new owner
        # will redo the work from the last checkpoint; nothing more to do here.
        log.warning("run %s: lost the lease before closing it", run.id)


def graph_input(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    config: RunnableConfig,
    run: queue.Run,
) -> Any:
    """What to pass to graph.invoke for this run, given the thread's checkpoint.

    A run may be on its second (or later) attempt after a worker died, so the
    checkpoint, not the attempt number, decides.
    """
    state = graph.get_state(config)
    if state.created_at is None:
        # No checkpoint yet: the graph never started on this thread.
        if run.kind == "resume":
            raise ValueError(f"resume for thread {run.thread_id!r}, which has no checkpoint")
        return run.payload  # a start run's original input
    if run.kind == "resume" and state.interrupts:
        # Still waiting at the interrupt: the resume value was never applied
        # (first attempt, or a crash inside the resumed step). Apply it.
        return Command(resume=run.payload)
    # Steps are pending from an earlier attempt that died: continue from the
    # last checkpoint. Sending the input or resume value again would apply it
    # twice.
    return None


def _heartbeat(
    engine: Engine, run: queue.Run, lease_seconds: float, interval: float, done: threading.Event
) -> None:
    while not done.wait(interval):
        try:
            still_ours = queue.extend(engine, run, lease_seconds)
        except Exception:
            # We can't prove we still hold the lease, so act as if we don't.
            log.exception("run %s: could not extend the lease", run.id)
            still_ours = False
        if not still_ours:
            log.critical("run %s: lease lost; exiting now (self-fencing)", run.id)
            # os._exit, not sys.exit: sys.exit in this thread would end only
            # this thread, and the graph would carry on. os._exit stops the
            # whole process at once, without finishing the step, writing a
            # checkpoint or closing the run. The new owner does all of that.
            os._exit(LOST_LEASE_EXIT)


def _stop(beat: threading.Thread, done: threading.Event) -> None:
    done.set()
    if beat is not threading.current_thread():
        beat.join()
