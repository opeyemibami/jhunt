"""The worker: one test per resume case, plus errors, the heartbeat and the loop.

A crash is simulated in-process with Crash, a BaseException: it passes through
process_run (which only handles Exception), so the run is left 'running', as if
the worker had died. The test then expires the lease and lets a worker retry.
The real-process versions are in tests/guarantees/.
"""

import operator
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import enqueue_resume, enqueue_start
from jhunt.platform.runtime.worker import process_run, run_worker
from tests.runtime.lease_watch import LeaseWatch


class Crash(BaseException):
    """Stands in for the worker process dying mid-step."""


class State(TypedDict, total=False):
    trail: Annotated[list[str], operator.add]
    answer: str
    answer2: str


# Which nodes ran, in order, and which node should crash on its next run.
executed: list[str] = []
crash_next: set[str] = set()


def step(name: str, work: Any = None):  # type: ignore[no-untyped-def]
    def node(state: State) -> State:
        executed.append(name)
        update: State = {"trail": [name]}
        if work is not None:
            update.update(work(state))
        if name in crash_next:
            crash_next.discard(name)
            raise Crash(name)
        return update

    return node


def build_linear(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = StateGraph(State)
    for name in ("step1", "step2", "step3"):
        graph.add_node(name, step(name))
    graph.add_edge(START, "step1")
    graph.add_edge("step1", "step2")
    graph.add_edge("step2", "step3")
    graph.add_edge("step3", END)
    return graph.compile(checkpointer=checkpointer)


def build_ask(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = StateGraph(State)
    graph.add_node("before", step("before"))
    graph.add_node("ask", step("ask", lambda s: {"answer": interrupt("question?")}))
    graph.add_node("after", step("after"))
    graph.add_edge(START, "before")
    graph.add_edge("before", "ask")
    graph.add_edge("ask", "after")
    graph.add_edge("after", END)
    return graph.compile(checkpointer=checkpointer)


def build_broken(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    def boom(state: State) -> State:
        raise ValueError("bad input")

    graph = StateGraph(State)
    graph.add_node("boom", boom)
    graph.add_edge(START, "boom")
    graph.add_edge("boom", END)
    return graph.compile(checkpointer=checkpointer)


def build_slow(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = StateGraph(State)
    graph.add_node("slow", step("slow", lambda s: time.sleep(2.5) or {}))
    graph.add_edge(START, "slow")
    graph.add_edge("slow", END)
    return graph.compile(checkpointer=checkpointer)


def build_twice(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    """Two interrupts in a row: ask -> ask2 -> after."""
    graph = StateGraph(State)
    graph.add_node("ask", step("ask", lambda s: {"answer": interrupt("first?")}))
    graph.add_node("ask2", step("ask2", lambda s: {"answer2": interrupt("second?")}))
    graph.add_node("after", step("after"))
    graph.add_edge(START, "ask")
    graph.add_edge("ask", "ask2")
    graph.add_edge("ask2", "after")
    graph.add_edge("after", END)
    return graph.compile(checkpointer=checkpointer)


# Every graph.invoke call on a "spied" graph, with its input.
invocations: list[Any] = []


def build_spied(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
    graph = build_linear(checkpointer)
    real_invoke = graph.invoke

    def invoke(graph_input: Any, *args: Any, **kwargs: Any) -> Any:
        invocations.append(graph_input)
        return real_invoke(graph_input, *args, **kwargs)

    graph.invoke = invoke  # type: ignore[method-assign]
    return graph


REGISTRY = Registry()
REGISTRY.register("linear", 1, build_linear)
REGISTRY.register("ask", 1, build_ask)
REGISTRY.register("broken", 1, build_broken)
REGISTRY.register("slow", 1, build_slow)
REGISTRY.register("twice", 1, build_twice)
REGISTRY.register("spied", 1, build_spied)


# The lease-lost handler for this test's in-process workers (see lease_watch).
watch: LeaseWatch


@pytest.fixture(autouse=True)
def reset(lease_watch: LeaseWatch) -> None:
    global watch
    watch = lease_watch
    executed.clear()
    crash_next.clear()
    invocations.clear()


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()


def work_once(engine: Engine, info: str, **kwargs: float) -> queue.Run:
    run = queue.claim(engine)
    assert run is not None, "nothing to claim"
    process_run(engine, info, REGISTRY, run, on_lease_lost=watch, **kwargs)
    return run


def crash_once(engine: Engine, info: str) -> queue.Run:
    run = queue.claim(engine)
    assert run is not None
    with pytest.raises(Crash):
        process_run(engine, info, REGISTRY, run, on_lease_lost=watch)
    # The worker "died": its lease runs out, so another worker may retry.
    expire(engine, run.id)
    return run


def crash_before_close(engine: Engine, info: str) -> queue.Run:
    """The graph stops normally, then the worker dies before closing the run."""
    run = queue.claim(engine)
    assert run is not None

    def die(_: queue.Run) -> None:
        raise Crash("before close")

    with pytest.raises(Crash):
        process_run(engine, info, REGISTRY, run, before_close=die, on_lease_lost=watch)
    expire(engine, run.id)
    return run


def expire(engine: Engine, run_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )


def status(engine: Engine, run_id: int) -> tuple[str, int, str | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, attempts, error FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    return row.status, row.attempts, row.error


def state(info: str, builder: Any, thread_id: str) -> Any:
    with Connection.connect(info, autocommit=True, row_factory=dict_row) as conn:
        return builder(PostgresSaver(conn)).get_state({"configurable": {"thread_id": thread_id}})


# --- the three resume cases -------------------------------------------------


def test_no_checkpoint_sends_the_original_input(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "linear", 1, "start", {"trail": ["input"]})
    work_once(engine, info)

    assert status(engine, run_id) == ("succeeded", 1, None)
    assert state(info, build_linear, "t1").values["trail"] == ["input", "step1", "step2", "step3"]


def test_pending_steps_continue_from_the_last_checkpoint(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "linear", 1, "start", {"trail": ["input"]})
    crash_next.add("step2")
    crash_once(engine, info)
    assert executed == ["step1", "step2"]

    work_once(engine, info)  # the retry

    # step1 is not re-run and the input is not sent again (it would appear twice).
    assert executed == ["step1", "step2", "step2", "step3"]
    assert status(engine, run_id) == ("succeeded", 2, None)
    assert state(info, build_linear, "t1").values["trail"] == ["input", "step1", "step2", "step3"]


def test_waiting_at_an_interrupt_resumes_with_the_payload(db: tuple[Engine, str]) -> None:
    engine, info = db
    start = enqueue_start(engine, REGISTRY, "t1", "ask", {"trail": []})
    work_once(engine, info)
    assert status(engine, start) == ("interrupted", 1, None)

    resume = enqueue_resume(engine, info, REGISTRY, "t1", "yes")
    work_once(engine, info)

    assert status(engine, resume) == ("succeeded", 1, None)
    final = state(info, build_ask, "t1")
    assert (final.values["answer"], final.values["trail"]) == ("yes", ["before", "ask", "after"])


def test_crash_inside_the_resumed_step_resumes_with_the_payload_again(
    db: tuple[Engine, str],
) -> None:
    engine, info = db
    enqueue_start(engine, REGISTRY, "t1", "ask", {"trail": []})
    work_once(engine, info)
    resume = enqueue_resume(engine, info, REGISTRY, "t1", "yes")

    crash_next.add("ask")  # dies after interrupt() returned, before the step was saved
    crash_once(engine, info)
    assert state(info, build_ask, "t1").interrupts  # still waiting at the interrupt

    work_once(engine, info)

    assert status(engine, resume) == ("succeeded", 2, None)
    assert state(info, build_ask, "t1").values["answer"] == "yes"


def test_crash_in_the_step_after_a_resume_continues_without_resending(
    db: tuple[Engine, str],
) -> None:
    engine, info = db
    enqueue_start(engine, REGISTRY, "t1", "ask", {"trail": []})
    work_once(engine, info)
    resume = enqueue_resume(engine, info, REGISTRY, "t1", "yes")

    crash_next.add("after")  # the resume was applied and saved; the next step dies
    crash_once(engine, info)
    after_crash = state(info, build_ask, "t1")
    assert (after_crash.interrupts, after_crash.next) == ((), ("after",))

    work_once(engine, info)

    assert status(engine, resume) == ("succeeded", 2, None)
    final = state(info, build_ask, "t1")
    assert (final.values["answer"], final.values["trail"]) == ("yes", ["before", "ask", "after"])
    assert executed.count("after") == 2  # only the interrupted step ran again


# --- a crash after the graph stopped, before the run was closed --------------


def test_enqueue_resume_records_the_interrupt_it_answers(db: tuple[Engine, str]) -> None:
    engine, info = db
    enqueue_start(engine, REGISTRY, "t1", "ask", {"trail": []})
    work_once(engine, info)
    pending = state(info, build_ask, "t1").interrupts[0]

    run_id = enqueue_resume(engine, info, REGISTRY, "t1", "yes")

    with engine.connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert payload == {"interrupt_id": pending.id, "value": "yes"}


def test_enqueue_resume_refuses_a_thread_that_is_not_waiting(db: tuple[Engine, str]) -> None:
    engine, info = db
    enqueue_start(engine, REGISTRY, "t1", "linear", {"trail": []})
    work_once(engine, info)  # runs to the end, never waits
    with pytest.raises(ValueError, match="not waiting at an interrupt"):
        enqueue_resume(engine, info, REGISTRY, "t1", "yes")


def test_resume_applied_then_crash_before_close_leaves_the_next_interrupt_unanswered(
    db: tuple[Engine, str],
) -> None:
    engine, info = db
    enqueue_start(engine, REGISTRY, "t1", "twice", {"trail": []})
    work_once(engine, info)  # waits at the first interrupt
    first = enqueue_resume(engine, info, REGISTRY, "t1", "first answer")

    # Attempt 1 answers the first interrupt, the graph stops at the second
    # one, and the worker dies before closing the run.
    crash_before_close(engine, info)
    second = state(info, build_twice, "t1").interrupts
    assert [i.value for i in second] == ["second?"]

    work_once(engine, info)  # the retry

    # The retry must not answer the second interrupt with the first answer.
    assert status(engine, first) == ("interrupted", 2, None)
    after = state(info, build_twice, "t1")
    assert [i.value for i in after.interrupts] == ["second?"]
    assert after.interrupts[0].id == second[0].id
    assert after.values.get("answer") == "first answer"
    assert "answer2" not in after.values
    assert executed.count("ask2") == 1  # not re-run by the retry

    # The second interrupt still takes its own answer.
    reply = enqueue_resume(engine, info, REGISTRY, "t1", "second answer")
    work_once(engine, info)
    assert status(engine, reply) == ("succeeded", 1, None)
    assert state(info, build_twice, "t1").values["answer2"] == "second answer"


def test_completed_then_crash_before_close_is_closed_without_invoking(
    db: tuple[Engine, str],
) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "spied", 1, "start", {"trail": ["input"]})

    crash_before_close(engine, info)  # the graph completed; the run is still open
    assert state(info, build_linear, "t1").next == ()

    work_once(engine, info)  # the retry

    assert status(engine, run_id) == ("succeeded", 2, None)
    assert invocations == [{"trail": ["input"]}]  # attempt 1 only; the retry didn't invoke
    assert executed == ["step1", "step2", "step3"]


# --- failures ---------------------------------------------------------------


def test_unregistered_workflow_fails_the_run(
    db: tuple[Engine, str], caplog: pytest.LogCaptureFixture
) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "nope", 7, "start", {})
    work_once(engine, info)

    assert status(engine, run_id) == ("failed", 1, "not registered: workflow 'nope' version 7")
    assert "not registered: workflow 'nope' version 7" in caplog.text


def test_graph_error_fails_the_run_with_its_message(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "broken", 1, "start", {})
    work_once(engine, info)
    assert status(engine, run_id) == ("failed", 1, "ValueError: bad input")


def test_resume_for_a_thread_with_no_checkpoint_fails(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "never-started", "ask", 1, "resume", "yes")
    work_once(engine, info)
    run_status, _, error = status(engine, run_id)
    assert run_status == "failed"
    assert "no checkpoint" in str(error)


# --- heartbeat and loop -----------------------------------------------------


def test_heartbeat_keeps_the_lease_during_a_long_step(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "slow", 1, "start", {})
    run = queue.claim(engine, lease_seconds=1)
    assert run is not None
    worker = threading.Thread(
        target=process_run,
        args=(engine, info, REGISTRY, run),
        kwargs={"lease_seconds": 1, "heartbeat_seconds": 0.3, "on_lease_lost": watch},
    )
    worker.start()
    time.sleep(1.5)  # longer than the lease: only the heartbeat keeps it alive
    assert queue.claim(engine, lease_seconds=1) is None
    worker.join()
    assert status(engine, run_id) == ("succeeded", 1, None)


def test_loop_drains_the_queue_until_stopped(db: tuple[Engine, str]) -> None:
    engine, info = db
    ids = [queue.enqueue(engine, f"t{i}", "linear", 1, "start", {"trail": []}) for i in range(3)]
    stop = watch.stop  # a lost lease also stops the loop
    loop = threading.Thread(
        target=run_worker,
        args=(engine, info, REGISTRY),
        kwargs={"idle_seconds": 0.05, "stop": stop, "on_lease_lost": watch},
    )
    loop.start()
    deadline = time.monotonic() + 30
    while any(status(engine, i)[0] != "succeeded" for i in ids):
        assert time.monotonic() < deadline, "the loop did not finish the runs"
        time.sleep(0.05)
    stop.set()
    loop.join(timeout=10)
    assert not loop.is_alive()


def test_registry_refuses_to_replace_a_version() -> None:
    registry = Registry()
    registry.register("w", 1, build_linear)
    with pytest.raises(ValueError, match="register a new version"):
        registry.register("w", 1, build_ask)
    assert registry.get("w", 1) is build_linear
    assert registry.get("w", 2) is None


# --- the lease-lost handler -------------------------------------------------


def test_lost_lease_calls_the_handler_and_the_heartbeat_stops(db: tuple[Engine, str]) -> None:
    engine, info = db
    run_id = queue.enqueue(engine, "t1", "slow", 1, "start", {})
    run = queue.claim(engine, lease_seconds=1)
    assert run is not None
    lost: list[tuple[int, str]] = []
    worker = threading.Thread(
        target=process_run,
        args=(engine, info, REGISTRY, run),
        kwargs={
            "lease_seconds": 1,
            "heartbeat_seconds": 0.3,
            "on_lease_lost": lambda r, reason: lost.append((r.id, reason)),
        },
    )
    worker.start()
    # Another worker takes the run over while the slow step is still running.
    expire(engine, run_id)
    taken = queue.claim(engine, lease_seconds=30)
    assert taken is not None
    worker.join()

    assert lost == [(run_id, "the run is no longer ours: another worker took it over")]
    # The fenced close did nothing: the run still belongs to the new owner.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, lease_owner FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    assert (row.status, row.lease_owner) == ("running", taken.owner)


def test_lease_watch_fails_the_test_with_the_run_and_reason() -> None:
    from tests.runtime.lease_watch import LeaseWatch

    watch = LeaseWatch()
    watch.check()  # nothing lost: passes
    run = queue.Run(7, "t", "w", 1, "start", {}, 1, uuid.uuid4())
    watch(run, "extend failed: OperationalError: connection lost")
    assert watch.stop.is_set()
    with pytest.raises(pytest.fail.Exception, match="run 7: extend failed: OperationalError"):
        watch.check()
