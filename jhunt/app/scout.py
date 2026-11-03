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
    try:  # the Scout config first: a bad file stops it before anything else
        scout.load_sources(settings.jhunt_config_dir)
        scout.load_scope(settings.jhunt_config_dir)
        scout.load_targets(settings.jhunt_config_dir)
    except (scout.SourcesError, scout.ScopeError, scout.TargetsError) as exc:
        raise SystemExit(f"jhunt-scout: {exc}") from None
    engine = create_engine(settings.database_url())
    registry = build_registry(settings, engine)
    day = today()
    mode: scout.Mode = "retry" if args.retry else "rescore" if args.rescore else "run"
    try:
        thread_id, boards = scout.start_scout(
            engine, registry, settings.jhunt_config_dir, day, mode
        )
    except (scout.SourcesError, scout.ScopeError, scout.TargetsError) as exc:
        raise SystemExit(f"jhunt-scout: {exc}") from None
    except scout.ScoutRefused as refused:
        print(_refusal(refused, mode))
        return
    print(f"queued: thread {thread_id} ({boards} boards)")
    if args.wait:
        conninfo = settings.database_url().set(drivername="postgresql")
        _wait(engine, conninfo.render_as_string(hide_password=False), registry, thread_id)


def _refusal(refused: scout.ScoutRefused, mode: scout.Mode) -> str:
    """The command's wording for each refusal (the digest has its own)."""
    latest = refused.latest
    if refused.reason == "none_today":
        return f"nothing to {mode}: no Scout run today; run jhunt-scout"
    assert latest is not None
    if refused.reason == "already":
        if latest.status == "failed":
            return (
                f"today's run failed: thread {latest.thread_id}; jhunt-scout --retry starts another"
            )
        return (
            f"already ran today: thread {latest.thread_id} ({latest.status}); Scout runs once a day"
        )
    if refused.reason == "not_failed":
        state = "succeeded" if latest.status == "succeeded" else f"is {latest.status}"
        return f"not retrying: today's run {state} (thread {latest.thread_id})"
    state = "failed" if latest.status == "failed" else f"is {latest.status}"
    hint = "; use --retry" if latest.status == "failed" else ""
    return f"not rescoring: today's run {state} (thread {latest.thread_id}){hint}"


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
