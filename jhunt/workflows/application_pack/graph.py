"""The Application pack graphs: version 1, draft (ADR 0016); version 2,
requirements then draft (ADR 0019). Version 1 stays registered, unchanged,
for threads that started on it (hard rule 12).

One thread per draft: pack-<application_id>-<n>, a new n each time the owner
asks, so earlier drafts are kept. The one step calls the draft_pack tool
through the tool gateway (step "draft"). It's safe to re-run (AGENTS.md):
the gateway returns the recorded result, so the model is called once.
"""

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jhunt.platform.gateway import ToolGateway

WORKFLOW = "application_pack"


class PackState(TypedDict, total=False):
    application_id: int  # the input
    requirements: dict[str, Any]  # v2: extract_requirements' result: kept, dropped, summary
    draft: dict[str, Any]  # draft_pack's result: draft_id, accepted, blocked


def thread_id_for(application_id: int, n: int) -> str:
    return f"pack-{application_id}-{n}"


def build(gateway: ToolGateway):  # type: ignore[no-untyped-def]
    """A registry builder for Application pack v1, wired to this gateway."""

    def draft(state: PackState, config: RunnableConfig) -> PackState:
        thread_id = config["configurable"]["thread_id"]
        result = gateway.call(
            thread_id,
            "draft",
            "draft",
            "draft_pack",
            {"application_id": state["application_id"], "thread_id": thread_id},
            run_id=config["configurable"].get("run_id"),
        )
        return {"draft": result}

    def builder(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(PackState)
        graph.add_node("draft", draft)
        graph.add_edge(START, "draft")
        graph.add_edge("draft", END)
        return graph.compile(checkpointer=checkpointer)

    return builder


def build_v2(gateway: ToolGateway):  # type: ignore[no-untyped-def]
    """A registry builder for Application pack v2: requirements, then draft.
    Each step is one gateway call, so a re-run gets the recorded result and
    the model is called once per step."""

    def call(tool: str, step: str, state: PackState, config: RunnableConfig) -> dict[str, Any]:
        thread_id = config["configurable"]["thread_id"]
        args = {"application_id": state["application_id"], "thread_id": thread_id}
        return gateway.call(thread_id, step, step, tool, args,
                            run_id=config["configurable"].get("run_id"))  # fmt: skip

    def requirements(state: PackState, config: RunnableConfig) -> PackState:
        return {"requirements": call("extract_requirements", "requirements", state, config)}

    def draft(state: PackState, config: RunnableConfig) -> PackState:
        return {"draft": call("draft_pack", "draft", state, config)}

    def builder(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(PackState)
        graph.add_node("requirements", requirements)
        graph.add_node("draft", draft)
        graph.add_edge(START, "requirements")
        graph.add_edge("requirements", "draft")
        graph.add_edge("draft", END)
        return graph.compile(checkpointer=checkpointer)

    return builder


# Every registered version and its builder; never edit one in place (rule 12).
VERSIONS = {1: build, 2: build_v2}
