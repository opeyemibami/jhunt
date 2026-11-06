"""The draft_pack tool: one application's pack, drafted and stored (ADR 0016).

It runs through the tool gateway under draft_application_pack, level 0:
suggest only. It only stores a draft for the owner (suggests=True), so the
gateway lets it run; nothing is sent or changed anywhere else.

In one tool call it reads the application's snapshot text (by exact
generation, sha256 checked), calls the model through the model gateway (twice at most: the
draft and, if anything was blocked, its one repair round), runs every claim
through the claim validator and stores the draft. Its result is
a reference and two counts (ADR 0008), recorded by the gateway: a re-run of
the step gets that result back, so the model is called once per thread.

The extract_requirements tool (v2, ADR 0019) runs first, under its own level
0 action: it lists the job's requirements, checks them and stores them for
the thread. draft_pack then aims the draft at them, if the thread has them.
"""

from typing import Any

from sqlalchemy import Engine, Row, text

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import Tool
from jhunt.platform.memory.claims import ClaimLevels
from jhunt.platform.memory.facts import latest_facts
from jhunt.workflows.application_pack import requirements
from jhunt.workflows.application_pack.pack import ModelCall, draft_pack, store_draft

ACTION = "draft_application_pack"
REQUIREMENTS_ACTION = "extract_requirements"


def extract_requirements_tool(engine: Engine, store: RecordStore, call_model: ModelCall) -> Tool:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        application_id, thread_id = args["application_id"], args["thread_id"]
        found, job = _job(engine, store, application_id)
        facts = latest_facts(engine)
        response = call_model(
            requirements.build_request(facts, job, company=found.company, role=found.role)
        )
        if response.stop != "end":
            raise requirements.RequirementsReplyError(
                f"requirements: the model stopped early ({response.stop})"
            )
        checked = requirements.check_requirements(response.text, job, facts)
        requirements.store_requirements(engine, checked, thread_id=thread_id,
                                        record=response.record)  # fmt: skip
        with engine.connect() as conn:  # what's stored: an earlier attempt's, if any
            stored = requirements.load_requirements(conn, thread_id)
        if stored is None:
            raise RuntimeError(f"{thread_id}: requirements not stored")
        return {
            "kept": len(stored.requirements),
            "dropped": stored.dropped,
            "summary": requirements.summary(stored.requirements),
        }

    return Tool("extract_requirements", REQUIREMENTS_ACTION, run, suggests=True)


def draft_pack_tool(
    engine: Engine, store: RecordStore, call_model: ModelCall, levels: ClaimLevels
) -> Tool:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        application_id, thread_id = args["application_id"], args["thread_id"]
        found, job = _job(engine, store, application_id)
        with engine.connect() as conn:
            found_requirements = requirements.load_requirements(conn, thread_id)  # None on v1
        aim = (
            ""
            if found_requirements is None
            else requirements.requirements_block(found_requirements.requirements)
        )
        draft = draft_pack(call_model, latest_facts(engine), job, levels, company=found.company,
                           role=found.role, aim=aim)  # fmt: skip
        draft_id = store_draft(
            engine,
            draft,
            application_id=application_id,
            thread_id=thread_id,
            snapshot_id=found.snapshot_id,
        )
        checked = list(draft.final.values())  # the repaired round, if there was one
        return {
            "draft_id": draft_id,
            "repaired": draft.repaired,
            "accepted": sum(len(c.accepted) for c in checked),
            "blocked": sum(len(c.blocked) for c in checked),
        }

    return Tool("draft_pack", ACTION, run, suggests=True)


def _job(engine: Engine, store: RecordStore, application_id: int) -> tuple[Row[Any], str]:
    """The application, and its snapshot's text read by exact generation."""
    with engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT a.company, a.role, s.id AS snapshot_id, s.text_uri,"
                " s.text_generation, s.text_sha256"
                " FROM applications a JOIN jd_snapshots s ON s.id = a.snapshot_id"
                " WHERE a.id = :id"
            ),
            {"id": application_id},
        ).first()
    if found is None:
        raise LookupError(f"no application {application_id}")
    key = _key(found.text_uri)
    return found, store.get(key, found.text_generation, found.text_sha256).decode("utf-8")


def _key(uri: str) -> str:
    """gs://bucket/snapshots/<key>/page.txt -> snapshots/<key>/page.txt."""
    if not uri.startswith("gs://") or "/" not in uri[5:]:
        raise ValueError(f"unexpected snapshot uri {uri!r}")
    return uri[5:].split("/", 1)[1]
