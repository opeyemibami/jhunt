"""The worker loop: claim a run, run its graph, close the run (ADR 0005).

While a run is in progress, a heartbeat thread extends the lease. If the lease
can't be extended, another worker may already own the run, so this process
exits on the spot (self-fencing) instead of carrying on beside it.

What "exits on the spot" does is the lease-lost handler. In production it is
always exit_now (os._exit). Tests that run the worker inside the pytest process
pass a handler that records the loss instead, so a lost lease fails one named
test rather than killing the test run (ADR 0005).
"""

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine

from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import check_versions

log = logging.getLogger(__name__)

# Extend the lease every 10 s; it lasts 30 s, so two heartbeats can be missed.
HEARTBEAT_SECONDS = 10.0
# Exit code when the lease is lost: 75 is EX_TEMPFAIL, "try again later".
LOST_LEASE_EXIT = 75

# Called with the run and the reason when the heartbeat can't keep the lease.
LeaseLostHandler = Callable[[queue.Run, str], None]


def exit_now(run: queue.Run, reason: str) -> NoReturn:
    """The production lease-lost handler: end the process at once.

    os._exit, not sys.exit: sys.exit in the heartbeat thread would end only
    that thread, and the graph would carry on. os._exit stops the whole
    process without finishing the step, writing a checkpoint or closing the
    run. The new owner does all of that.
    """
    log.critical("run %s: lease lost (%s); exiting now (self-fencing)", run.id, reason)
    os._exit(LOST_LEASE_EXIT)


def run_worker(
    engine: Engine,
    conninfo: str,
    registry: Registry,
    *,
    lease_seconds: float = queue.LEASE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    idle_seconds: float = 1.0,
    stop: threading.Event | None = None,
    on_lease_lost: LeaseLostHandler = exit_now,
) -> None:
    """Claim and process runs until `stop` is set. One run at a time.

    Raises VersionChanged, before claiming anything, if a registered version's
    graph differs from the one first recorded for it (hard rule 12).
    """
    check_versions(engine, registry)
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
            on_lease_lost=on_lease_lost,
        )


def process_run(
    engine: Engine,
    conninfo: str,
    registry: Registry,
    run: queue.Run,
    *,
    lease_seconds: float = queue.LEASE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    before_close: Callable[[queue.Run], None] | None = None,
    on_lease_lost: LeaseLostHandler = exit_now,
) -> None:
    """Run one claimed run's graph and close the run.

    before_close is a test hook. It runs after the graph has stopped and before
    the run is closed, which is the window the crash-before-close tests need.
    """
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
        args=(engine, run, lease_seconds, heartbeat_seconds, done, on_lease_lost),
        daemon=True,
    )
    beat.start()
    try:
        # The checkpointer's connection settings AGENTS.md requires.
        with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
            graph = builder(PostgresSaver(conn))
            config: RunnableConfig = {"configurable": {"thread_id": run.thread_id}}
            plan = plan_run(graph, config, run)
            if plan.close_as is None:
                # durability="sync": each checkpoint is saved before the next
                # step starts, so a crash loses at most the step in progress.
                graph.invoke(plan.input, config, durability="sync")
                waiting = bool(graph.get_state(config).interrupts)
                status = "interrupted" if waiting else "succeeded"
            else:
                status = plan.close_as  # an earlier attempt already did the work
    except Exception as exc:
        log.exception("run %s failed", run.id)
        _stop(beat, done)
        queue.fail(engine, run, f"{type(exc).__name__}: {exc}")
        return
    finally:
        _stop(beat, done)

    if before_close is not None:
        before_close(run)
    if not queue.finish(engine, run, status):
        # The lease was lost between the last heartbeat and now. The new owner
        # will redo the work from the last checkpoint; nothing more to do here.
        log.warning("run %s: lost the lease before closing it", run.id)


@dataclass(frozen=True)
class Plan:
    """What to do with a claimed run, decided from the thread's checkpoint."""

    input: Any = None  # passed to graph.invoke, when close_as is None
    close_as: str | None = None  # close the run with this status without invoking


def plan_run(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    config: RunnableConfig,
    run: queue.Run,
) -> Plan:
    """Decide what this run still has to do (ADR 0005, "What to send").

    A run may be on a later attempt after a worker died, at any point: mid-step,
    or after the graph stopped but before the run was closed. So the checkpoint,
    not the attempt number, decides.
    """
    state = graph.get_state(config)
    if state.created_at is None:
        # No checkpoint yet: the graph never started on this thread.
        if run.kind == "resume":
            raise ValueError(f"resume for thread {run.thread_id!r}, which has no checkpoint")
        return Plan(input=run.payload)  # a start run's original input

    if run.kind == "resume":
        answers = run.payload["interrupt_id"]
        if any(pending.id == answers for pending in state.interrupts):
            # The interrupt this run answers is still waiting: answer it by id,
            # so the value can only ever go to that interrupt.
            return Plan(input=Command(resume={answers: run.payload["value"]}))

    # From here on, the run's own input or answer is already in the checkpoint:
    # an earlier attempt applied it and died before closing the run.
    if state.interrupts:
        # The graph moved on and stopped at a later interrupt. Nothing to run.
        return Plan(close_as="interrupted")
    if state.next:
        # Steps are pending: continue from the last checkpoint. Sending the
        # input or the answer again would apply it twice.
        return Plan(input=None)
    # No pending steps and no interrupt: the graph already completed.
    return Plan(close_as="succeeded")


def _heartbeat(
    engine: Engine,
    run: queue.Run,
    lease_seconds: float,
    interval: float,
    done: threading.Event,
    on_lease_lost: LeaseLostHandler,
) -> None:
    while not done.wait(interval):
        try:
            still_ours = queue.extend(engine, run, lease_seconds)
            reason = "the run is no longer ours: another worker took it over"
        except Exception as exc:
            # We can't prove we still hold the lease, so act as if we don't.
            log.exception("run %s: could not extend the lease", run.id)
            still_ours = False
            reason = f"extend failed: {type(exc).__name__}: {exc}"
        if not still_ours:
            on_lease_lost(run, reason)  # in production this never returns
            return  # the heartbeat stops either way


def _stop(beat: threading.Thread, done: threading.Event) -> None:
    done.set()
    if beat is not threading.current_thread():
        beat.join()
