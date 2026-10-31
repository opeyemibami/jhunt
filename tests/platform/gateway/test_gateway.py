"""Gateway v0: idempotent tool calls with recorded results, level checks from
config, and an append-only audit row per call (ADR 0008)."""

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.platform.gateway import (
    IdempotencyConflict,
    Tool,
    ToolGateway,
    ToolRefused,
    idempotency_key,
)


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


class Counter:
    """A tool body that counts its runs and returns something unique per run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(args)
        return {"echo": args, "run": uuid.uuid4().hex}


def gateway_with(engine: Engine, body: Any, action: str = "fetch_postings") -> ToolGateway:
    return ToolGateway(engine, [Tool("fetch_posting", action, body)])


def rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(text("SELECT * FROM tool_calls ORDER BY id")).mappings()
        ]


def call(gw: ToolGateway, step: str = "page", args: dict[str, Any] | None = None) -> Any:
    return gw.call(
        "thread-1", "fetch", step, "fetch_posting", args or {"url": "https://x/1"}, run_id=5
    )


def test_idempotency_key_comes_only_from_thread_node_and_step() -> None:
    assert idempotency_key("t", "fetch", "page") == idempotency_key("t", "fetch", "page")
    assert idempotency_key("t", "fetch", "page") != idempotency_key("t", "fetch", "page2")
    assert idempotency_key("t", "fetch", "page") != idempotency_key("t2", "fetch", "page")
    # Joined unambiguously: ("a:b", "c") and ("a", "b:c") are different keys.
    assert idempotency_key("a:b", "c", "s") != idempotency_key("a", "b:c", "s")
    for empty in [("", "n", "s"), ("t", "", "s"), ("t", "n", "")]:
        with pytest.raises(ValueError):
            idempotency_key(*empty)


def test_a_repeated_call_returns_the_recorded_result_without_running(engine: Engine) -> None:
    body = Counter()
    gw = gateway_with(engine, body)
    first = call(gw)
    second = call(gw)

    assert first == second
    assert len(body.calls) == 1
    [row] = rows(engine)
    assert row["result"] == first
    assert (row["tool"], row["level"], row["outcome"], row["run_id"]) == (
        "fetch_posting",
        1,
        "succeeded",
        5,
    )
    assert (row["thread_id"], row["node"], row["step_key"]) == ("thread-1", "fetch", "page")
    assert row["idempotency_key"] == idempotency_key("thread-1", "fetch", "page")
    assert row["approved_by"] is None


def test_different_steps_are_different_calls(engine: Engine) -> None:
    body = Counter()
    gw = gateway_with(engine, body)
    call(gw, step="page-1")
    call(gw, step="page-2")
    assert len(body.calls) == 2
    assert len(rows(engine)) == 2


def test_two_concurrent_calls_on_one_key_return_the_same_recorded_result(engine: Engine) -> None:
    # Two workers running the same step at once (ADR 0005's self-fencing gap).
    both_running = threading.Barrier(2)
    body = Counter()

    def slow_body(args: dict[str, Any]) -> dict[str, Any]:
        both_running.wait(timeout=10)  # make sure both really run the tool
        return body(args)

    gw = gateway_with(engine, slow_body)
    results: list[Any] = [None, None]
    errors: list[BaseException] = []

    def caller(i: int) -> None:
        try:
            results[i] = call(gw)
        except BaseException as exc:  # surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []  # the loser never raises
    assert len(body.calls) == 2  # at most twice
    assert results[0] == results[1]  # both get the winner's recorded result
    [row] = rows(engine)
    assert row["result"] == results[0]


def test_same_key_with_different_args_is_refused(engine: Engine) -> None:
    gw = gateway_with(engine, Counter())
    call(gw, args={"url": "https://x/1"})
    with pytest.raises(IdempotencyConflict):
        call(gw, args={"url": "https://x/2"})


def test_a_failed_tool_records_nothing_and_runs_again_next_time(engine: Engine) -> None:
    attempts: list[int] = []

    def flaky(args: dict[str, Any]) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("network")
        return {"ok": True}

    gw = gateway_with(engine, flaky)
    with pytest.raises(TimeoutError):
        call(gw)
    assert rows(engine) == []
    assert call(gw) == {"ok": True}
    assert len(attempts) == 2


def test_levels_come_from_autonomy_settings(engine: Engine) -> None:
    body = Counter()
    gw = gateway_with(engine, body)
    # The latest row for the action wins; history is kept.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO autonomy_settings (action, level, changed_by)"
                " VALUES ('fetch_postings', 0, 'owner')"
            )
        )
    with pytest.raises(ToolRefused, match="level 0"):
        call(gw)
    assert body.calls == []
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO autonomy_settings (action, level, changed_by)"
                " VALUES ('fetch_postings', 1, 'owner')"
            )
        )
    call(gw)
    assert len(body.calls) == 1


def test_an_action_without_a_setting_is_refused(engine: Engine) -> None:
    body = Counter()
    gw = gateway_with(engine, body, action="send_followup")
    with pytest.raises(ToolRefused, match="no autonomy setting"):
        call(gw)
    assert body.calls == []


def test_an_unknown_tool_is_refused(engine: Engine) -> None:
    gw = gateway_with(engine, Counter())
    with pytest.raises(ToolRefused, match="no tool"):
        gw.call("t", "n", "s", "submit_application", {}, run_id=None)


def test_seeded_levels(engine: Engine) -> None:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT action, level FROM autonomy_settings"))
        seeded = {r.action: r.level for r in result}
    assert seeded == {"fetch_postings": 1, "snapshot_job_description": 1}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tool_calls SET outcome = 'failed'",
        "DELETE FROM tool_calls",
        "TRUNCATE tool_calls",
        "UPDATE autonomy_settings SET level = 2",
        "DELETE FROM autonomy_settings",
    ],
)
def test_audit_and_settings_history_never_change(engine: Engine, statement: str) -> None:
    call(gateway_with(engine, Counter()))
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text(statement))
