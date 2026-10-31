"""Threads and their pinned versions (ADR 0006).

A thread runs the workflow version it started on, for its whole life (AGENTS.md
hard rule 12). enqueue_start pins it; enqueue_resume reads the pin, so no
caller can choose a version for an existing thread. check_versions refuses a
registered version whose graph was changed in place.
"""

import hashlib
import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine, text

from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Builder, Registry


class ThreadExists(Exception):
    """enqueue_start was called for a thread that has already started."""


class VersionChanged(Exception):
    """A registered version's graph differs from the one first recorded for it."""


def enqueue_start(
    engine: Engine, registry: Registry, thread_id: str, workflow: str, graph_input: Any
) -> int:
    """Start a new thread on the workflow's latest version. Returns the run id."""
    version = registry.latest(workflow)
    if version is None:
        raise ValueError(f"not registered: workflow {workflow!r}")
    # One transaction: the pin and the first run exist together or not at all.
    with engine.begin() as conn:
        pinned = conn.execute(
            text(
                "INSERT INTO threads (thread_id, workflow, version)"
                " VALUES (:thread_id, :workflow, :version)"
                " ON CONFLICT (thread_id) DO NOTHING RETURNING thread_id"
            ),
            {"thread_id": thread_id, "workflow": workflow, "version": version},
        ).first()
        if pinned is None:
            raise ThreadExists(thread_id)
        return queue.insert_run(conn, thread_id, workflow, version, "start", graph_input)


def enqueue_resume(
    engine: Engine, conninfo: str, registry: Registry, thread_id: str, value: Any
) -> int:
    """Queue a resume run that answers the interrupt the thread waits at now.

    The version is the thread's pinned one. The run records that interrupt's
    id, so its value can only ever answer that interrupt (ADR 0005).
    """
    with engine.connect() as conn:
        pin = conn.execute(
            text("SELECT workflow, version FROM threads WHERE thread_id = :t"), {"t": thread_id}
        ).first()
    if pin is None:
        raise ValueError(f"unknown thread {thread_id!r}: it was never started")
    workflow, version = pin.workflow, pin.version
    builder = registry.get(workflow, version)
    if builder is None:
        raise ValueError(f"not registered: workflow {workflow!r} version {version}")

    with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as pg:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        pending = builder(PostgresSaver(pg)).get_state(config).interrupts
    if len(pending) != 1:
        raise ValueError(
            f"thread {thread_id!r} is not waiting at an interrupt"
            if not pending
            else f"thread {thread_id!r} waits at {len(pending)} interrupts; expected one"
        )
    payload = {"interrupt_id": pending[0].id, "value": value}
    return queue.enqueue(engine, thread_id, workflow, version, "resume", payload)


def fingerprint(builder: Builder) -> str:
    """sha256 of a graph's node names and edges.

    It catches nodes and edges added, removed, renamed or rewired. It does not
    catch a change to the code inside a node (ADR 0006).
    """
    graph = builder(InMemorySaver()).get_graph()  # no database needed to inspect it
    shape = {
        "nodes": sorted(graph.nodes),
        "edges": sorted([e.source, e.target, e.conditional] for e in graph.edges),
    }
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()


def check_versions(engine: Engine, registry: Registry) -> None:
    """Record each registered version's fingerprint the first time it's seen;
    raise VersionChanged if any differs from what was recorded before."""
    changed = []
    for (workflow, version), builder in registry.items():
        current = fingerprint(builder)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO graph_versions (workflow, version, fingerprint)"
                    " VALUES (:w, :v, :f) ON CONFLICT (workflow, version) DO NOTHING"
                ),
                {"w": workflow, "v": version, "f": current},
            )
            recorded = conn.execute(
                text("SELECT fingerprint FROM graph_versions WHERE workflow = :w AND version = :v"),
                {"w": workflow, "v": version},
            ).scalar_one()
        if recorded != current:
            changed.append(f"{workflow!r} version {version}")
    if changed:
        raise VersionChanged(
            f"graph changed in place for {', '.join(changed)}: "
            "register a new version instead (hard rule 12)"
        )
