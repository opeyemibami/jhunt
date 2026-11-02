"""Cover-note paragraphs and the drift rule (owner, after the first real
pack). Each cover-note sentence is tagged with a group (experience, projects,
community); the note has one paragraph per group, in that order, leaving out
groups with no accepted sentence. The prompt forbids changing a fact's actor
or the strength of its verb. That one can't be checked: the validator sees
citations, numbers and level words, not meaning (ADR 0015, ADR 0016)."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.memory.claims import Claim, check_claims, load_claim_levels
from jhunt.platform.memory.facts import FactVersion
from jhunt.platform.models import CallRecord, ModelRequest, ModelResponse
from jhunt.workflows.application_pack.pack import (
    PARAGRAPHS,
    PackReplyError,
    build_repair_request,
    build_request,
    cover_note_text,
    draft_pack,
    load_pack_config,
    note_paragraphs,
    parse_reply,
    store_draft,
)
from jhunt.workflows.capture.record import record_capture

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
LEVELS = load_claim_levels(REPO_CONFIG)
PROFILING = "GPU profiling (memory use, throughput, bottlenecks) was a standard part of every cycle"
FACTS = [
    FactVersion(1, "role-acme", 1, "role", "ML engineer at Acme; cut inference cost by 35%",
                "letter", date(2026, 10, 30), "verified", None),
    FactVersion(2, "setu-gpu-profiling", 1, "project", PROFILING, "lab notes",
                date(2026, 10, 30), "verified", None),
    FactVersion(3, "role-author", 1, "role", "Technical author for the MLOps Community",
                "articles", date(2026, 10, 30), "verified", None),
]  # fmt: skip
DRIFT_RULE = "Never change the actor or the strength of a fact's verb"


def reply(note: list[tuple[str, list[str], str]], cv: list[tuple[str, list[str]]] = ()) -> str:  # type: ignore[assignment]
    return json.dumps(
        {
            "cv_lines": [{"text": t, "fact_ids": i} for t, i in cv],
            "cover_note": [{"text": t, "fact_ids": i, "group": g} for t, i, g in note],
        }
    )


class Model:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []

    def __call__(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        record = CallRecord("anthropic", "claude-sonnet-5-5", request.step, 1, 0, 0, 1, 1, None)
        return ModelResponse(text=self.replies.pop(0), stop="end", record=record)


def test_paragraphs_are_the_groups_in_order_and_empty_ones_are_left_out() -> None:
    assert PARAGRAPHS == ("experience", "projects", "community")
    body = reply([
        ("I write for the MLOps Community.", ["role-author"], "community"),
        ("At Acme I cut inference cost by 35%.", ["role-acme"], "experience"),
        ("At Acme I cut inference cost by 50%.", ["role-acme"], "experience"),  # blocked
        ("I worked at Acme.", ["role-acme"], "experience"),
    ])  # fmt: skip
    model = Model(body, body)  # the stand-in repeats itself in the repair round
    draft = draft_pack(model, FACTS, "A job.", LEVELS, company="Fin", role="AI Engineer")
    paragraphs = note_paragraphs(draft)
    # Experience first, then community; no projects paragraph; no blocked text.
    assert paragraphs == [
        ["At Acme I cut inference cost by 35%.", "I worked at Acme."],
        ["I write for the MLOps Community."],
    ]
    note = cover_note_text(paragraphs, load_pack_config(REPO_CONFIG), company="Fin",
                           role="AI Engineer")  # fmt: skip
    body_text = note.split("\n\n")
    assert "At Acme I cut inference cost by 35%. I worked at Acme." in body_text
    assert "I write for the MLOps Community." in body_text
    assert body_text.index("At Acme I cut inference cost by 35%. I worked at Acme.") < (
        body_text.index("I write for the MLOps Community.")
    )
    assert "50%" not in note


@pytest.mark.parametrize(
    "body",
    [
        reply([("I worked at Acme.", ["role-acme"], "hobbies")]),  # not a group
        json.dumps({"cv_lines": [], "cover_note": [{"text": "x", "fact_ids": []}]}),  # no group
        json.dumps({"cv_lines": [{"text": "x", "fact_ids": [], "group": "experience"}],
                    "cover_note": []}),  # CV lines have no group
    ],
)  # fmt: skip
def test_a_sentence_must_have_a_known_group_and_a_cv_line_none(body: str) -> None:
    with pytest.raises(PackReplyError):
        parse_reply(body)


def test_the_repair_prompt_shows_each_sentences_group() -> None:
    first = reply([("At Acme I cut inference cost by 50%.", ["role-acme"], "experience")])
    model = Model(first, reply([("At Acme I cut inference cost by 35%.", ["role-acme"],
                                 "experience")]))  # fmt: skip
    draft_pack(model, FACTS, "A job.", LEVELS, company="Fin", role="AI Engineer")
    assert "(experience) At Acme I cut inference cost by 50%." in model.requests[1].messages[0].text


def test_both_prompts_forbid_changing_a_facts_actor_or_verb() -> None:
    draft_request = build_request(FACTS, "A job.", company="Fin", role="AI Engineer")
    claims, groups = parse_reply(reply([("x", ["role-acme"], "experience")]))
    first = {
        "cv_line": check_claims(claims["cv_lines"], FACTS, LEVELS),
        "cover_note": check_claims(claims["cover_note"], FACTS, LEVELS),
    }
    repair_request = build_repair_request(FACTS, "A job.", first, groups, company="Fin", role="X")
    for request in (draft_request, repair_request):
        assert request.system is not None
        assert DRIFT_RULE in request.system
        assert "'was a standard part of' must not become 'made'" in request.system


def test_drift_is_a_known_limit_the_validator_cannot_see() -> None:
    # The drift from application 6's first draft. Its citation, its numbers
    # and its level words are all fine, so it passes: only the prompt rule
    # guards against it, until the meaning check (backlog, evals).
    drifted = Claim("Made GPU profiling a standard part of every cycle", ("setu-gpu-profiling",))
    [checked] = check_claims([drifted], FACTS, LEVELS).claims
    assert not checked.blocked


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def test_each_sentences_group_is_stored(engine: Engine) -> None:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url="https://x/1", company="Fin", title="AI Engineer", dedupe_key="6" * 64,
        snapshot=refs,
    )  # fmt: skip
    with engine.connect() as conn:
        snapshot_id = conn.execute(text("SELECT snapshot_id FROM applications")).scalar_one()
    body = reply([("I write for the MLOps Community.", ["role-author"], "community")],
                 [("Cut inference cost by 35% at Acme", ["role-acme"])])  # fmt: skip
    draft = draft_pack(Model(body), FACTS, "A job.", LEVELS, company="Fin", role="X")
    store_draft(engine, draft, application_id=application_id, thread_id="pack-p-1",
                snapshot_id=snapshot_id)  # fmt: skip
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT section, paragraph FROM draft_claims ORDER BY section")
        ).all()
    assert [tuple(r) for r in rows] == [("cover_note", "community"), ("cv_line", None)]
