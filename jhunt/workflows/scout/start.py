"""Starting today's Scout run: the once-a-day rule, Retry and Rescore (ADR 0011).

Shared by jhunt-scout and the digest's "Run Scout now" (ADR 0017), so both
follow the same rules:
- one run a day: scout-<day>, then scout-<day>-2, -3 for later attempts;
- a new run only if there's none today;
- Retry only if today's latest run failed;
- Rescore only if today's latest run succeeded.
The thread's input is read from sources.toml, scope.toml and targets.toml now.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine, text

from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows.scout.fit import load_targets
from jhunt.workflows.scout.graph import VERSION, WORKFLOW, scout_input, summary, thread_id_for
from jhunt.workflows.scout.location import load_scope
from jhunt.workflows.scout.sources import load_sources

Mode = Literal["run", "retry", "rescore"]


def utc_today() -> str:
    """Scout's day: the UTC date, the same as Ireland's for a morning run."""
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class Today:
    """Today's latest Scout attempt."""

    thread_id: str
    attempt: int
    status: str
    run_id: int


class ScoutRefused(Exception):
    """Not started, and why. reason: "already" (a run today, no Retry or
    Rescore asked), "none_today", "not_failed" (Retry) or "not_succeeded"
    (Rescore)."""

    def __init__(self, reason: str, latest: Today | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.latest = latest


def latest_today(engine: Engine, day: str) -> Today | None:
    first = thread_id_for(day)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, thread_id, status FROM runs"
                " WHERE workflow = :w AND (thread_id = :first OR thread_id LIKE :retries)"
                " ORDER BY id"
            ),
            {"w": WORKFLOW, "first": first, "retries": f"{first}-%"},
        ).all()
    latest = None
    for run_id, thread_id, status in rows:
        attempt = 1 if thread_id == first else int(thread_id.removeprefix(f"{first}-"))
        if latest is None or attempt >= latest.attempt:
            latest = Today(thread_id, attempt, status, run_id)
    return latest


def start_scout(
    engine: Engine, registry: Registry, config_dir: Path, day: str, mode: Mode
) -> tuple[str, int]:
    """Queue today's run if the rules allow it: (thread id, number of boards).
    Raises ScoutRefused, or SourcesError, ScopeError or TargetsError for a
    bad config file, before anything is queued."""
    sources = load_sources(config_dir)
    scope = load_scope(config_dir)
    targets = load_targets(config_dir)
    latest = latest_today(engine, day)
    if mode == "run":
        if latest is not None:
            raise ScoutRefused("already", latest)
        thread_id = thread_id_for(day)
    else:
        if latest is None:
            raise ScoutRefused("none_today", None)
        wanted = "failed" if mode == "retry" else "succeeded"
        if latest.status != wanted:
            raise ScoutRefused("not_failed" if mode == "retry" else "not_succeeded", latest)
        thread_id = thread_id_for(day, latest.attempt + 1)
    try:
        enqueue_start(engine, registry, thread_id, WORKFLOW, scout_input(day, sources, scope,
                                                                          targets))  # fmt: skip
    except ThreadExists:  # another request queued it just now
        raise ScoutRefused("already", latest_today(engine, day)) from None
    return thread_id, len(sources)


def run_summary(conninfo: str, registry: Registry, thread_id: str) -> list[str]:
    """A finished run's summary lines, from its graph state."""
    builder = registry.get(WORKFLOW, VERSION)
    if builder is None:
        return []
    with Connection.connect(conninfo, autocommit=True, row_factory=dict_row) as pg:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        state: dict[str, Any] = builder(PostgresSaver(pg)).get_state(config).values
    return summary(state) if state.get("day") else []
