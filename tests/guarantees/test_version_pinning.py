"""Guarantee: an open application survives a workflow change.

Ten threads start on v1 and stop at an interrupt. Then v2 is deployed next to
v1 (a worker whose registry has both). Every resume of the ten runs on v1, the
version each thread started on; a new thread starts on v2. A worker also
refuses to start if a registered version's graph was changed in place.
"""

import operator
import threading
import time
from collections.abc import Callable, Iterator
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import (
    VersionChanged,
    check_versions,
    enqueue_resume,
    enqueue_start,
)
from jhunt.platform.runtime.worker import run_worker

WORKFLOW = "pinned"


class State(TypedDict, total=False):
    trail: Annotated[list[str], operator.add]
    answer: str


def graph_with(
    prefix: str, extra: bool = False
) -> Callable[[BaseCheckpointSaver], CompiledStateGraph]:  # type: ignore[type-arg]
    """ask (interrupt) -> done, with node names that show the version that ran."""

    def build(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(State)
        graph.add_node(
            f"{prefix}_ask", lambda s: {"trail": [f"{prefix}_ask"], "answer": interrupt("?")}
        )
        graph.add_node(f"{prefix}_done", lambda s: {"trail": [f"{prefix}_done"]})
        graph.add_edge(START, f"{prefix}_ask")
        graph.add_edge(f"{prefix}_ask", f"{prefix}_done")
        if extra:  # the same version, changed in place: one more node
            graph.add_node(f"{prefix}_extra", lambda s: {"trail": [f"{prefix}_extra"]})
            graph.add_edge(f"{prefix}_done", f"{prefix}_extra")
            graph.add_edge(f"{prefix}_extra", END)
        else:
            graph.add_edge(f"{prefix}_done", END)
        return graph.compile(checkpointer=checkpointer)

    return build


build_v1, build_v2 = graph_with("v1"), graph_with("v2")


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()


def run_until(engine: Engine, info: str, registry: Registry, done: Callable[[], bool]) -> None:
    """Run a worker loop in a thread until `done()` holds, then stop it."""
    stop = threading.Event()
    loop = threading.Thread(
        target=run_worker,
        args=(engine, info, registry),
        kwargs={"idle_seconds": 0.05, "stop": stop},
    )
    loop.start()
    deadline = time.monotonic() + 60
    try:
        while not done():
            assert loop.is_alive(), "the worker loop stopped early"
            assert time.monotonic() < deadline, "timed out"
            time.sleep(0.05)
    finally:
        stop.set()
        loop.join(timeout=30)


def statuses(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT status, count(*) AS n FROM runs GROUP BY status"))
        return {r.status: r.n for r in rows}


def trail(info: str, builder: Callable[..., CompiledStateGraph], thread_id: str) -> list[str]:  # type: ignore[type-arg]
    with Connection.connect(info, autocommit=True, row_factory=dict_row) as conn:
        state = builder(PostgresSaver(conn)).get_state({"configurable": {"thread_id": thread_id}})
    return list(state.values["trail"])


def waiting_at(
    info: str, builder: Callable[..., CompiledStateGraph], thread_id: str
) -> tuple[str, ...]:  # type: ignore[type-arg]
    with Connection.connect(info, autocommit=True, row_factory=dict_row) as conn:
        state = builder(PostgresSaver(conn)).get_state({"configurable": {"thread_id": thread_id}})
    return tuple(state.next)


def test_open_threads_finish_on_the_version_they_started_on(db: tuple[Engine, str]) -> None:
    engine, info = db
    threads = [f"app-{i}" for i in range(10)]

    v1_only = Registry()
    v1_only.register(WORKFLOW, 1, build_v1)
    for thread_id in threads:
        enqueue_start(engine, v1_only, thread_id, WORKFLOW, {"trail": []})
    run_until(engine, info, v1_only, lambda: statuses(engine) == {"interrupted": 10})

    # Deploy v2 next to v1.
    deployed = Registry()
    deployed.register(WORKFLOW, 1, build_v1)
    deployed.register(WORKFLOW, 2, build_v2)
    for thread_id in threads:
        enqueue_resume(engine, info, deployed, thread_id, "yes")
    enqueue_start(engine, deployed, "app-new", WORKFLOW, {"trail": []})
    run_until(
        engine, info, deployed, lambda: statuses(engine) == {"interrupted": 11, "succeeded": 10}
    )

    for thread_id in threads:
        assert trail(info, build_v1, thread_id) == ["v1_ask", "v1_done"]
    # The new thread runs v2: it waits at v2's interrupt (a node that calls
    # interrupt() writes its trail entry only once it is resumed).
    assert waiting_at(info, build_v2, "app-new") == ("v2_ask",)
    with engine.connect() as conn:
        versions = {
            r.thread_id: r.version
            for r in conn.execute(text("SELECT thread_id, version FROM threads"))
        }
        run_versions = set(
            conn.execute(
                text("SELECT DISTINCT version FROM runs WHERE thread_id <> 'app-new'")
            ).scalars()
        )
    assert {t: versions[t] for t in threads} == dict.fromkeys(threads, 1)
    assert versions["app-new"] == 2
    assert run_versions == {1}


def test_worker_refuses_a_version_changed_in_place(db: tuple[Engine, str]) -> None:
    engine, info = db
    original = Registry()
    original.register(WORKFLOW, 1, build_v1)
    check_versions(engine, original)  # a first worker records v1's fingerprint
    enqueue_start(engine, original, "app-1", WORKFLOW, {"trail": []})

    changed = Registry()
    changed.register(WORKFLOW, 1, graph_with("v1", extra=True))  # v1 edited, not a new version
    # If the check were missing, the loop would run on; stop it after 2 s so the
    # test fails ("DID NOT RAISE") instead of hanging.
    stop = threading.Event()
    timer = threading.Timer(2.0, stop.set)
    timer.start()
    try:
        with pytest.raises(VersionChanged, match="'pinned' version 1"):
            run_worker(engine, info, changed, idle_seconds=0.05, stop=stop)
    finally:
        timer.cancel()

    assert statuses(engine) == {"queued": 1}  # it refused before claiming anything
