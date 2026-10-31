"""The run queue: leases, SKIP LOCKED claims, fencing, one active run per thread."""

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.platform.runtime import queue


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def add(engine: Engine, thread_id: str, kind: str = "start") -> int:
    return queue.enqueue(engine, thread_id, "hello", 1, kind, {"n": 1})


def row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as conn:
        r = conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}).mappings().one()
    return dict(r)


def expire(engine: Engine, run_id: int) -> None:
    """Move a lease into the past, as if its worker died long ago."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )


def test_enqueue_then_claim(engine: Engine) -> None:
    run_id = add(engine, "t1")
    run = queue.claim(engine)
    assert run is not None
    assert (run.id, run.thread_id, run.workflow, run.version) == (run_id, "t1", "hello", 1)
    assert (run.kind, run.payload, run.attempts) == ("start", {"n": 1}, 1)

    stored = row(engine, run_id)
    assert stored["status"] == "running"
    assert stored["lease_owner"] == run.owner
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT lease_until - now() FROM runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert 25 < left.total_seconds() <= 30  # the default 30 s lease, on the database clock


def test_empty_queue_returns_none(engine: Engine) -> None:
    assert queue.claim(engine) is None


def test_claim_skips_a_row_another_worker_holds(engine: Engine) -> None:
    first = add(engine, "t1")
    second = add(engine, "t2")
    # Another worker is mid-claim on the first run and holds its row lock.
    with engine.connect() as other:
        other.execute(text("SELECT id FROM runs WHERE id = :id FOR UPDATE"), {"id": first})
        run = queue.claim(engine)  # SKIP LOCKED: doesn't wait, takes the next one
        assert run is not None and run.id == second
        other.rollback()


def test_concurrent_claims_never_share_a_run(engine: Engine) -> None:
    ids = {add(engine, f"t{i}") for i in range(40)}

    def drain() -> list[int]:
        got = []
        while (run := queue.claim(engine)) is not None:
            got.append(run.id)
        return got

    with ThreadPoolExecutor(max_workers=4) as pool_:
        results = list(pool_.map(lambda _: drain(), range(4)))
    claimed = [i for r in results for i in r]
    assert sorted(claimed) == sorted(ids)  # every run once, none twice


def test_second_run_on_a_thread_waits_for_the_first(engine: Engine) -> None:
    first = add(engine, "t1")
    second = add(engine, "t1", kind="resume")

    run = queue.claim(engine)
    assert run is not None and run.id == first
    assert queue.claim(engine) is None  # one active run per thread: the resume waits

    assert queue.finish(engine, run, "interrupted")
    resumed = queue.claim(engine)
    assert resumed is not None and resumed.id == second


def test_expired_lease_is_reclaimed_with_a_new_owner(engine: Engine) -> None:
    run_id = add(engine, "t1")
    first = queue.claim(engine)
    assert first is not None
    assert queue.claim(engine) is None  # still leased

    expire(engine, run_id)
    second = queue.claim(engine)
    assert second is not None
    assert (second.id, second.attempts) == (run_id, 2)
    assert second.owner != first.owner


def test_stale_owner_can_no_longer_touch_the_run(engine: Engine) -> None:
    run_id = add(engine, "t1")
    stale = queue.claim(engine)
    assert stale is not None
    expire(engine, run_id)
    current = queue.claim(engine)
    assert current is not None

    # Fencing: every write is conditional on the owner token.
    assert not queue.extend(engine, stale)
    assert not queue.finish(engine, stale, "succeeded")
    assert not queue.fail(engine, stale, "boom")
    assert row(engine, run_id)["status"] == "running"

    assert queue.extend(engine, current)
    assert queue.finish(engine, current, "succeeded")
    assert row(engine, run_id)["status"] == "succeeded"


def test_extend_pushes_the_lease_out(engine: Engine) -> None:
    run_id = add(engine, "t1")
    run = queue.claim(engine, lease_seconds=5)
    assert run is not None
    assert queue.extend(engine, run, lease_seconds=60)
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT lease_until - now() FROM runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert left.total_seconds() > 55


def test_finish_and_fail_close_the_run(engine: Engine) -> None:
    ok, bad = add(engine, "t1"), add(engine, "t2")
    run_ok, run_bad = queue.claim(engine), queue.claim(engine)
    assert run_ok is not None and run_bad is not None

    assert queue.finish(engine, run_ok, "succeeded")
    assert queue.fail(engine, run_bad, "ValueError: bad input")

    done, failed = row(engine, ok), row(engine, bad)
    assert (done["status"], done["lease_owner"], done["lease_until"]) == ("succeeded", None, None)
    assert (failed["status"], failed["error"]) == ("failed", "ValueError: bad input")


def test_finish_only_accepts_closing_statuses(engine: Engine) -> None:
    add(engine, "t1")
    run = queue.claim(engine)
    assert run is not None
    with pytest.raises(ValueError):
        queue.finish(engine, run, "queued")


def test_exhausted_run_is_failed_by_the_claim_path(engine: Engine) -> None:
    run_id = add(engine, "t1")
    for _ in range(queue.MAX_ATTEMPTS):
        assert queue.claim(engine) is not None
        expire(engine, run_id)

    assert queue.claim(engine) is None  # never a sixth attempt
    failed = row(engine, run_id)
    assert (failed["status"], failed["attempts"]) == ("failed", queue.MAX_ATTEMPTS)
    assert "gave up after 5 attempts" in str(failed["error"])


def test_later_resume_proceeds_once_the_exhausted_run_is_failed(engine: Engine) -> None:
    # Without the claim path failing it, the exhausted run would stay the
    # oldest open run on its thread and block every later run forever.
    stuck = add(engine, "t1")
    for _ in range(queue.MAX_ATTEMPTS):
        assert queue.claim(engine) is not None
        expire(engine, stuck)
    later = add(engine, "t1", kind="resume")

    run = queue.claim(engine)
    assert run is not None and run.id == later
    assert row(engine, stuck)["status"] == "failed"


def test_database_refuses_two_running_runs_on_one_thread(engine: Engine) -> None:
    a, b = add(engine, "t1"), add(engine, "t1")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = 'running', lease_owner = :o WHERE id IN (:a, :b)"),
            {"o": uuid.uuid4(), "a": a, "b": b},
        )
