"""A real worker process for the crash tests. Test fixture only.

    python -m tests.runtime.worker_fixture

It runs the real loop (jhunt.platform.runtime.worker.run_worker) with a test
registry holding one graph, "crashy" v1: step1 -> step2 -> step3. Each node
records (thread, node, pid) in a probe table, then adds "node@pid" to the
state's trail, so the checkpoints show which process ran each step.

With HANG_IN_STEP2=1, step2 records its start and then waits for a release row
that the tests never write: the process is killed or stopped while it waits.

The database comes from the POSTGRES_* variables. Lease and heartbeat come from
TEST_LEASE_SECONDS and TEST_HEARTBEAT_SECONDS.
"""

import logging
import operator
import os
import signal
import sys
import threading
import time
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg import Connection
from sqlalchemy import create_engine

from jhunt.platform.config import get_settings
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.worker import run_worker

WORKFLOW = "crashy"
PROBE_TABLE = "crash_probe"
RELEASE_TABLE = "crash_release"
PROBE_DDL = [
    f"CREATE TABLE {PROBE_TABLE} (id bigserial PRIMARY KEY, thread_id text NOT NULL,"
    " node text NOT NULL, pid integer NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp())",
    f"CREATE TABLE {RELEASE_TABLE} (thread_id text PRIMARY KEY)",
]
PROBE_DROP = [f"DROP TABLE IF EXISTS {PROBE_TABLE}", f"DROP TABLE IF EXISTS {RELEASE_TABLE}"]


class State(TypedDict):
    trail: Annotated[list[str], operator.add]


def conninfo() -> str:
    url = get_settings().database_url().set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def record(thread_id: str, node: str) -> None:
    # Own autocommit connection: the row must survive a SIGKILL a moment later.
    with Connection.connect(conninfo(), autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO {PROBE_TABLE} (thread_id, node, pid) VALUES (%s, %s, %s)",
            (thread_id, node, os.getpid()),
        )


def released(thread_id: str) -> bool:
    with Connection.connect(conninfo(), autocommit=True) as conn:
        cur = conn.execute(f"SELECT 1 FROM {RELEASE_TABLE} WHERE thread_id = %s", (thread_id,))
        return cur.fetchone() is not None


def make_node(name: str):  # type: ignore[no-untyped-def]
    def node(state: State, config: RunnableConfig) -> dict[str, list[str]]:
        thread_id = config["configurable"]["thread_id"]
        record(thread_id, name)
        if name == "step2" and os.environ.get("HANG_IN_STEP2") == "1":
            while not released(thread_id):  # the test's signal or kill ends this
                time.sleep(0.05)
        return {"trail": [f"{name}@{os.getpid()}"]}

    return node


def build(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = StateGraph(State)
    for name in ("step1", "step2", "step3"):
        graph.add_node(name, make_node(name))
    graph.add_edge(START, "step1")
    graph.add_edge("step1", "step2")
    graph.add_edge("step2", "step3")
    graph.add_edge("step3", END)
    return graph.compile(checkpointer=checkpointer)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    registry = Registry()
    registry.register(WORKFLOW, 1, build)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run_worker(
        create_engine(get_settings().database_url()),
        conninfo(),
        registry,
        lease_seconds=float(os.environ["TEST_LEASE_SECONDS"]),
        heartbeat_seconds=float(os.environ["TEST_HEARTBEAT_SECONDS"]),
        idle_seconds=0.1,
        stop=stop,
    )


if __name__ == "__main__":
    main()
