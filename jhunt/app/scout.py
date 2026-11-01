"""`jhunt-scout [--retry | --rescore] [--wait]`: read today's job boards (ADR 0011).

It queues one Scout run for today over the boards in sources.toml, with the
settings in scope.toml and the targets in targets.toml; a running jhunt-worker
does the work. With --wait it
waits for the run and prints what it found. Scout runs once a day: asking
again the same day says so. Only if today's run failed does --retry start
another (thread scout-<day>-2, -3 and so on). Only if today's run succeeded
does --rescore start another, which scores today's board lists again from
staging, without fetching: for postings stored before they were scored, or
after targets.toml changes.
"""

import argparse
import time
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine, create_engine, text

from jhunt.app.registry import build_registry
from jhunt.platform.config import get_settings
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import scout

WAIT_SECONDS = 300


def today() -> str:
    """The UTC date: the same as Ireland's for a morning run."""
    return datetime.now(UTC).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-scout", description=__doc__.split("\n")[0])
    again = parser.add_mutually_exclusive_group()
    again.add_argument("--retry", action="store_true", help="run again if today's run failed")
    again.add_argument(
        "--rescore", action="store_true", help="score today's postings again after a success"
    )
    parser.add_argument("--wait", action="store_true", help="wait for the run and print it")
    args = parser.parse_args()

    settings = get_settings()
    try:
        sources = scout.load_sources(settings.jhunt_config_dir)
        scope = scout.load_scope(settings.jhunt_config_dir)
        targets = scout.load_targets(settings.jhunt_config_dir)
    except (scout.SourcesError, scout.ScopeError, scout.TargetsError) as exc:
        raise SystemExit(f"jhunt-scout: {exc}") from None

    engine = create_engine(settings.database_url())
    registry = build_registry(settings, engine)
    day = today()
    latest = _latest_today(engine, day)
    if args.retry:
        if latest is None:
            print("nothing to retry: no Scout run today; run jhunt-scout")
            return
        thread_id, attempt, status = latest
        if status != "failed":
            state = "succeeded" if status == "succeeded" else f"is {status}"
            print(f"not retrying: today's run {state} (thread {thread_id})")
            return
        thread_id = scout.thread_id_for(day, attempt + 1)
    elif args.rescore:
        if latest is None:
            print("nothing to rescore: no Scout run today; run jhunt-scout")
            return
        thread_id, attempt, status = latest
        if status != "succeeded":
            state = "failed" if status == "failed" else f"is {status}"
            hint = "; use --retry" if status == "failed" else ""
            print(f"not rescoring: today's run {state} (thread {thread_id}){hint}")
            return
        thread_id = scout.thread_id_for(day, attempt + 1)
    elif latest is not None:
        thread_id, _, status = latest
        if status == "failed":
            print(f"today's run failed: thread {thread_id}; jhunt-scout --retry starts another")
        else:
            print(f"already ran today: thread {thread_id} ({status}); Scout runs once a day")
        return
    else:
        thread_id = scout.thread_id_for(day)
    try:
        enqueue_start(
            engine,
            registry,
            thread_id,
            scout.WORKFLOW,
            scout.scout_input(day, sources, scope, targets),
        )
    except ThreadExists:  # another jhunt-scout queued it just now
        print(f"already queued: thread {thread_id}")
        return
    print(f"queued: thread {thread_id} ({len(sources)} boards)")
    if args.wait:
        conninfo = settings.database_url().set(drivername="postgresql")
        _wait(engine, conninfo.render_as_string(hide_password=False), registry, thread_id)


def _latest_today(engine: Engine, day: str) -> tuple[str, int, str] | None:
    """Today's latest Scout thread, its attempt number and its run's status."""
    first = scout.thread_id_for(day)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT thread_id, status FROM runs"
                " WHERE workflow = :w AND (thread_id = :first OR thread_id LIKE :retries)"
                " ORDER BY id"
            ),
            {"w": scout.WORKFLOW, "first": first, "retries": f"{first}-%"},
        ).all()
    latest = None
    for thread_id, status in rows:
        attempt = 1 if thread_id == first else int(thread_id.removeprefix(f"{first}-"))
        if latest is None or attempt >= latest[1]:
            latest = (thread_id, attempt, status)
    return latest


def _wait(engine: Engine, conninfo: str, registry: Registry, thread_id: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            run = conn.execute(
                text("SELECT status, error FROM runs WHERE thread_id = :t"), {"t": thread_id}
            ).one()
        if run.status == "failed":
            raise SystemExit(f"jhunt-scout: the run failed: {run.error}")
        if run.status == "succeeded":
            builder = registry.get(scout.WORKFLOW, scout.VERSION)  # the version just queued
            assert builder is not None  # registered by build_registry
            with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as pg:
                config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
                state = builder(PostgresSaver(pg)).get_state(config).values
            print("\n".join(scout.summary(state)))
            return
        time.sleep(1)
    raise SystemExit("jhunt-scout: still running; is jhunt-worker running?")
