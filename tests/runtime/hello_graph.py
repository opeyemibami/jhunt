"""A three-step graph that records every node execution. Test fixture only.

Run as a separate process so a test can SIGKILL it mid-step:

    python -m tests.runtime.hello_graph run <thread_id>      # fresh run
    python -m tests.runtime.hello_graph resume <thread_id>   # continue a thread

The database comes from the usual POSTGRES_* variables. With HELLO_HANG_IN=<node>,
that node records its start and then blocks until the process is killed.
"""

import itertools
import operator
import os
import signal
import sys
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg import Connection
from psycopg.rows import dict_row

from jhunt.platform.config import get_settings

PROBE_TABLE = "hello_probe"
NODES = ("step1", "step2", "step3")


class State(TypedDict):
    trail: Annotated[list[str], operator.add]


def conninfo() -> str:
    url = get_settings().database_url().set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def record(node: str, thread_id: str) -> None:
    # Own autocommit connection: the row must survive a SIGKILL a moment later.
    with Connection.connect(conninfo(), autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO {PROBE_TABLE} (thread_id, node, pid) VALUES (%s, %s, %s)",
            (thread_id, node, os.getpid()),
        )


def make_node(name: str):  # type: ignore[no-untyped-def]
    # config must be annotated RunnableConfig, or LangGraph doesn't pass it.
    def node(state: State, config: RunnableConfig) -> dict[str, list[str]]:
        record(name, config["configurable"]["thread_id"])
        if os.environ.get("HELLO_HANG_IN") == name:
            signal.pause()  # wait here for the test's SIGKILL
        return {"trail": [name]}

    return node


def build(checkpointer: PostgresSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = StateGraph(State)
    for name in NODES:
        graph.add_node(name, make_node(name))
    graph.add_edge(START, NODES[0])
    for a, b in itertools.pairwise(NODES):
        graph.add_edge(a, b)
    graph.add_edge(NODES[-1], END)
    return graph.compile(checkpointer=checkpointer)


def open_checkpointer(info: str) -> tuple[Connection, PostgresSaver]:  # type: ignore[type-arg]
    # The connection settings AGENTS.md requires for the checkpointer.
    conn = Connection.connect(info, autocommit=True, row_factory=dict_row)
    return conn, PostgresSaver(conn)


def main(argv: list[str]) -> int:
    mode, thread_id = argv
    conn, saver = open_checkpointer(conninfo())
    with conn:
        graph = build(saver)
        config = {"configurable": {"thread_id": thread_id}}
        # durability="sync": each checkpoint is written before the next step
        # starts (AGENTS.md). The default "async" can lose one on a crash.
        graph_input = {"trail": []} if mode == "run" else None
        graph.invoke(graph_input, config, durability="sync")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
