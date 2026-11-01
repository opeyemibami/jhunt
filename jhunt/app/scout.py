"""`jhunt-scout [--wait]`: read today's job boards (ADR 0011).

It queues one Scout run for today over the boards in sources.toml, with the
settings in scope.toml; a running jhunt-worker does the work. With --wait it
waits for the run and prints what it found. Scout runs once a day: asking
again the same day says so.
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
    parser.add_argument("--wait", action="store_true", help="wait for the run and print it")
    args = parser.parse_args()

    settings = get_settings()
    try:
        sources = scout.load_sources(settings.jhunt_config_dir)
        scope = scout.load_scope(settings.jhunt_config_dir)
    except (scout.SourcesError, scout.ScopeError) as exc:
        raise SystemExit(f"jhunt-scout: {exc}") from None

    engine = create_engine(settings.database_url())
    registry = build_registry(settings, engine)
    day = today()
    thread_id = scout.thread_id_for(day)
    try:
        enqueue_start(
            engine, registry, thread_id, scout.WORKFLOW, scout.scout_input(day, sources, scope)
        )
    except ThreadExists:
        print(f"already ran today: thread {thread_id}; Scout runs once a day")
        return
    print(f"queued: thread {thread_id} ({len(sources)} boards)")
    if args.wait:
        conninfo = settings.database_url().set(drivername="postgresql")
        _wait(engine, conninfo.render_as_string(hide_password=False), registry, thread_id)


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
            builder = registry.get(scout.WORKFLOW, scout.VERSION)
            assert builder is not None  # registered by build_registry
            with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as pg:
                config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
                state = builder(PostgresSaver(pg)).get_state(config).values
            print("\n".join(scout.summary(state)))
            return
        time.sleep(1)
    raise SystemExit("jhunt-scout: still running; is jhunt-worker running?")
