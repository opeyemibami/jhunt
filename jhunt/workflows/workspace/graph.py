"""The submission graph, version 1: record what the owner confirmed on the
application page (ADR 0018).

One thread per submission, submission_thread_id, the same as the command's.
The input is what the preview showed: the application, channel, date, and
each file's kind, name, type, size, sha256 and staged copy. Four steps, each
safe to re-run (AGENTS.md):

check   reads every staged copy and hashes it: any that is gone or isn't the
        sha256 shown fails the run before anything is written to records.
store   store_submission_file for each file, from its staged copy, through
        the gateway: the tool hashes what it read again and refuses a mismatch.
record  record_submission: returns the one it wrote if it already ran.
clean   deletes each staged copy, through the gateway.
"""

import hashlib
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import Engine, text

from jhunt.platform.artifacts import StagingStore
from jhunt.platform.gateway import ToolGateway
from jhunt.workflows.workspace.submission import (
    SubmissionFileChanged,
    SubmittedFile,
    read_staged,
    record_submission,
)

WORKFLOW = "submission"
VERSION = 1


class SubmissionState(TypedDict, total=False):
    # The input.
    application_id: int
    channel: str
    submitted_at: str
    files: list[dict[str, Any]]  # kind, file_name, content_type, size, sha256, key, generation
    # Filled in by the steps.
    stored: list[dict[str, Any]]  # store_submission_file's results, in the order of files
    submission_id: int


def build(engine: Engine, gateway: ToolGateway, staging: StagingStore):  # type: ignore[no-untyped-def]
    """A registry builder for submission v1, wired to this gateway and staging."""

    def check(state: SubmissionState) -> SubmissionState:
        for f in state["files"]:
            data = read_staged(staging, _staged(f), f["sha256"], f["file_name"])
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise SubmissionFileChanged(f"{f['key']} changed after the preview")
        return {}

    def store(state: SubmissionState, config: RunnableConfig) -> SubmissionState:
        thread_id = config["configurable"]["thread_id"]
        with engine.connect() as conn:
            dedupe_key = conn.execute(
                text("SELECT dedupe_key FROM applications WHERE id = :a"),
                {"a": state["application_id"]},
            ).scalar_one()
        stored = [
            gateway.call(
                thread_id,
                "store",
                f"{f['kind']}:{f['sha256']}",
                "store_submission_file",
                {
                    "staged": _staged(f),
                    "dedupe_key": dedupe_key,
                    "file_name": f["file_name"],
                    "content_type": f["content_type"],
                    "sha256": f["sha256"],
                },
                run_id=config["configurable"].get("run_id"),
            )
            for f in state["files"]
        ]
        return {"stored": stored}

    def record(state: SubmissionState, config: RunnableConfig) -> SubmissionState:
        files = [
            SubmittedFile(f["kind"], f["file_name"], f["content_type"], f["size"], ref)
            for f, ref in zip(state["files"], state["stored"], strict=True)
        ]
        submission_id, _ = record_submission(
            engine,
            submission_key=config["configurable"]["thread_id"],
            application_id=state["application_id"],
            channel=state["channel"],
            submitted_at=state["submitted_at"],
            files=files,
        )
        return {"submission_id": submission_id}

    def clean(state: SubmissionState, config: RunnableConfig) -> SubmissionState:
        for f in state["files"]:
            gateway.call(
                config["configurable"]["thread_id"], "clean", f"{f['sha256']}:{f['file_name']}",
                "delete_staged_submission_file", _staged(f),
                run_id=config["configurable"].get("run_id"),
            )  # fmt: skip
        return {}

    def builder(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:  # type: ignore[type-arg]
        graph = StateGraph(SubmissionState)
        graph.add_node("check", check)
        graph.add_node("store", store)
        graph.add_node("record", record)
        graph.add_node("clean", clean)
        graph.add_edge(START, "check")
        graph.add_edge("check", "store")
        graph.add_edge("store", "record")
        graph.add_edge("record", "clean")
        graph.add_edge("clean", END)
        return graph.compile(checkpointer=checkpointer)

    return builder


def _staged(f: dict[str, Any]) -> dict[str, Any]:
    return {"key": f["key"], "generation": f["generation"]}
