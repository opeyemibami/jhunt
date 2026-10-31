"""The Capture graph, version 1: snapshot -> record.

snapshot: the gateway runs snapshot_posting once per thread (step "page"); a
          re-run after a crash gets the recorded references back.
record:   writes the snapshot row, the application and its event (record.py).

Both steps are safe to re-run (AGENTS.md): the first through the gateway, the
second by checking for the application before writing anything.
"""

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import Engine

from jhunt.platform.gateway import ToolGateway
from jhunt.workflows.capture.record import record_capture

WORKFLOW = "capture"
VERSION = 1


class CaptureState(TypedDict, total=False):
    # The input, set when the thread starts.
    url: str
    company: str
    title: str
    dedupe_key: str
    # Filled in by the steps.
    snapshot: dict[str, Any]
    application_id: int
    already_captured: bool


def thread_id_for(dedupe_key: str) -> str:
    """One Capture thread per posting."""
    return f"capture-{dedupe_key}"


def build(engine: Engine, gateway: ToolGateway):  # type: ignore[no-untyped-def]
    """A registry builder for Capture v1, wired to this engine and gateway."""

    def snapshot(state: CaptureState, config: RunnableConfig) -> CaptureState:
        refs = gateway.call(
            config["configurable"]["thread_id"],
            "snapshot",
            "page",
            "snapshot_posting",
            {"url": state["url"], "dedupe_key": state["dedupe_key"]},
            run_id=config["configurable"].get("run_id"),
        )
        return {"snapshot": refs}

    def record(state: CaptureState) -> CaptureState:
        application_id, already = record_capture(
            engine,
            url=state["url"],
            company=state["company"],
            title=state["title"],
            dedupe_key=state["dedupe_key"],
            snapshot=state["snapshot"],
        )
        return {"application_id": application_id, "already_captured": already}

    def builder(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(CaptureState)
        graph.add_node("snapshot", snapshot)
        graph.add_node("record", record)
        graph.add_edge(START, "snapshot")
        graph.add_edge("snapshot", "record")
        graph.add_edge("record", END)
        return graph.compile(checkpointer=checkpointer)

    return builder
