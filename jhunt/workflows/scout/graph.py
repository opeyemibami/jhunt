"""The Scout graph, version 1: fetch -> store -> cleanup (ADR 0011).

One thread per day, scout-<day>. Its input fixes the day, the sources and the
scope settings, so a re-run after a crash reads the same boards the same way.

fetch:   the gateway runs fetch_board once per board (step "<provider>:<board>").
         A board that fails is listed in "failed"; the others go on.
store:   the gateway runs store_postings once per fetched board.
cleanup: deletes postings unseen for 30 days, unless captured.

All three are safe to re-run (AGENTS.md): the first two through the gateway,
which returns recorded results; cleanup because deleting again finds nothing.
A failed board isn't recorded, so a re-run tries it again.
"""

from collections.abc import Mapping
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import Engine, text

from jhunt.platform.gateway import ToolGateway
from jhunt.workflows.scout.fetch import BoardFetchFailed
from jhunt.workflows.scout.location import Scope
from jhunt.workflows.scout.sources import Source

WORKFLOW = "scout"
VERSION = 1
# Postings not seen on their board for this long are deleted, unless captured.
UNSEEN_DAYS = 30


class ScoutState(TypedDict, total=False):
    # The input, set when the thread starts (scout_input).
    day: str
    sources: list[dict[str, str]]
    northern_ireland_in_scope: bool
    # Filled in by the steps, keyed by "<provider>:<board>".
    fetched: dict[str, dict[str, Any]]
    failed: dict[str, str]
    stored: dict[str, dict[str, int]]
    removed: int


def thread_id_for(day: str) -> str:
    """One Scout thread per day."""
    return f"scout-{day}"


def scout_input(day: str, sources: list[Source], scope: Scope) -> ScoutState:
    return {
        "day": day,
        "sources": [
            {"provider": s.provider, "board": s.board, "company": s.company} for s in sources
        ],
        "northern_ireland_in_scope": scope.northern_ireland_in_scope,
    }


def summary(state: Mapping[str, Any]) -> list[str]:
    """What jhunt-scout prints when the run is done."""
    stored = state.get("stored", {}).values()
    failed = state.get("failed", {})
    lines = [
        f"scout {state['day']}: {len(state['sources'])} boards, {len(failed)} failed",
        f"postings: {sum(c['new'] for c in stored)} new,"
        f" {sum(c['seen_again'] for c in stored)} seen again,"
        f" {sum(c['in_scope'] for c in stored)} in scope",
        f"removed: {state.get('removed', 0)} unseen for {UNSEEN_DAYS} days",
    ]
    return lines + [f"failed: {name}: {error}" for name, error in failed.items()]


def build(engine: Engine, gateway: ToolGateway):  # type: ignore[no-untyped-def]
    """A registry builder for Scout v1, wired to this engine and gateway."""

    def fetch(state: ScoutState, config: RunnableConfig) -> ScoutState:
        fetched: dict[str, dict[str, Any]] = {}
        failed: dict[str, str] = {}
        for source in state["sources"]:
            name = f"{source['provider']}:{source['board']}"
            args = {"provider": source["provider"], "board": source["board"], "day": state["day"]}
            try:
                fetched[name] = gateway.call(
                    config["configurable"]["thread_id"],
                    "fetch",
                    name,
                    "fetch_board",
                    args,
                    run_id=config["configurable"].get("run_id"),
                )
            except BoardFetchFailed as exc:
                failed[name] = str(exc)
        return {"fetched": fetched, "failed": failed}

    def store(state: ScoutState, config: RunnableConfig) -> ScoutState:
        stored: dict[str, dict[str, int]] = {}
        for source in state["sources"]:
            name = f"{source['provider']}:{source['board']}"
            ref = state["fetched"].get(name)
            if ref is None:
                continue  # failed in fetch
            args = source | {
                "northern_ireland_in_scope": state["northern_ireland_in_scope"],
                "key": ref["key"],
                "generation": ref["generation"],
                "sha256": ref["sha256"],
            }
            stored[name] = gateway.call(
                config["configurable"]["thread_id"],
                "store",
                name,
                "store_postings",
                args,
                run_id=config["configurable"].get("run_id"),
            )
        return {"stored": stored}

    def cleanup(state: ScoutState) -> ScoutState:
        # A captured posting's row stays: the digest links it to its application.
        with engine.begin() as conn:
            removed = conn.execute(
                text(
                    "DELETE FROM postings p"
                    " WHERE p.last_seen_at < now() - make_interval(days => :days)"
                    " AND NOT EXISTS"
                    " (SELECT 1 FROM applications a WHERE a.dedupe_key = p.dedupe_key)"
                ),
                {"days": UNSEEN_DAYS},
            ).rowcount
        return {"removed": removed}

    def builder(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(ScoutState)
        graph.add_node("fetch", fetch)
        graph.add_node("store", store)
        graph.add_node("cleanup", cleanup)
        graph.add_edge(START, "fetch")
        graph.add_edge("fetch", "store")
        graph.add_edge("store", "cleanup")
        graph.add_edge("cleanup", END)
        return graph.compile(checkpointer=checkpointer)

    return builder
