"""Threads are pinned to the version they started on; versions are fingerprinted."""

import itertools
from collections.abc import Iterator

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import (
    ThreadExists,
    VersionChanged,
    check_versions,
    enqueue_resume,
    enqueue_start,
    fingerprint,
)


def graph(*nodes: str):  # type: ignore[no-untyped-def]
    def build(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        g = StateGraph(dict)
        for n in nodes:
            g.add_node(n, lambda s: {})
        g.add_edge(START, nodes[0])
        for a, b in itertools.pairwise(nodes):
            g.add_edge(a, b)
        g.add_edge(nodes[-1], END)
        return g.compile(checkpointer=checkpointer)

    return build


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def test_start_pins_the_latest_version(engine: Engine) -> None:
    registry = Registry()
    registry.register("w", 1, graph("a"))
    registry.register("w", 3, graph("b"))
    run_id = enqueue_start(engine, registry, "t1", "w", {"x": 1})

    with engine.connect() as conn:
        pinned = conn.execute(
            text("SELECT workflow, version FROM threads WHERE thread_id = 't1'")
        ).one()
        run = conn.execute(
            text("SELECT kind, version FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    assert tuple(pinned) == ("w", 3)
    assert tuple(run) == ("start", 3)


def test_starting_an_existing_thread_is_refused(engine: Engine) -> None:
    registry = Registry()
    registry.register("w", 1, graph("a"))
    enqueue_start(engine, registry, "t1", "w", {})
    with pytest.raises(ThreadExists):
        enqueue_start(engine, registry, "t1", "w", {})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1


def test_start_of_an_unregistered_workflow_is_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not registered"):
        enqueue_start(engine, Registry(), "t1", "nope", {})


def test_resume_of_an_unknown_thread_is_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="unknown thread"):
        enqueue_resume(engine, "unused", Registry(), "never-started", "yes")


def test_fingerprint_sees_nodes_and_edges() -> None:
    assert fingerprint(graph("a", "b")) == fingerprint(graph("a", "b"))
    assert fingerprint(graph("a", "b")) != fingerprint(graph("a", "c"))  # a renamed node
    assert fingerprint(graph("a", "b")) != fingerprint(graph("b", "a"))  # rewired edges


def test_check_versions_records_then_compares(engine: Engine) -> None:
    first = Registry()
    first.register("w", 1, graph("a"))
    check_versions(engine, first)
    check_versions(engine, first)  # the same graph again is fine

    second = Registry()
    second.register("w", 1, graph("a"))
    second.register("w", 2, graph("a", "b"))  # a new version is recorded, not refused
    check_versions(engine, second)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM graph_versions")).scalar_one() == 2

    changed = Registry()
    changed.register("w", 1, graph("a", "z"))
    with pytest.raises(VersionChanged, match="'w' version 1"):
        check_versions(engine, changed)
