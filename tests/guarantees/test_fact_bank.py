"""Guarantee: no claim without a fact (hard rule 6, ADR 0015).

Facts are imported into a scratch database the way jhunt-facts does it. Every
claim comes back, in order and with its text unchanged; a claim is blocked,
never reworded, when it cites no fact, an unknown fact or one that isn't
verified, has a number its cited facts don't, or states a skill above its
fact's level.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.memory.claims import Claim, check_claims, load_claim_levels
from jhunt.platform.memory.facts import import_facts, latest_facts, read_facts, retire_fact
from jhunt.platform.models import CallRecord, ModelRequest, ModelResponse
from jhunt.workflows.application_pack.pack import draft_pack, store_draft
from jhunt.workflows.capture.record import record_capture

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
FACTS = """
[[fact]]
id = "role-acme"
kind = "role"
text = "ML engineer at Acme from 2021 to 2024; cut inference cost by 35%; served 1,200 customers"
evidence = "reference letter"
verified_at = 2026-10-30
status = "verified"

[[fact]]
id = "skill-langgraph"
kind = "skill"
text = "LangGraph"
evidence = "github.com/me/jhunt"
verified_at = 2026-10-30
status = "verified"
level = "built"

[[fact]]
id = "skill-k8s"
kind = "skill"
text = "Kubernetes"
aliases = ["Kubernetes", "k8s"]
evidence = "course certificate and side project"
verified_at = 2026-10-30
status = "verified"
level = "used"

[[fact]]
id = "skill-fastapi"
kind = "skill"
text = "FastAPI"
evidence = "github.com/me/inferex"
verified_at = 2026-10-30
status = "verified"
level = "built"

[[fact]]
id = "project-inferex"
kind = "project"
text = "Inferex: an inference platform with a FastAPI gateway on Kubernetes"
evidence = "github.com/me/inferex"
verified_at = 2026-10-30
status = "verified"

[[fact]]
id = "skill-rust"
kind = "skill"
text = "Rust"
status = "draft"

[[fact]]
id = "project-old"
kind = "project"
text = "Recommendation engine at Acme"
evidence = "portfolio"
verified_at = 2026-10-30
status = "verified"
"""


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def bank(engine: Engine, tmp_path: Path) -> Engine:
    path = tmp_path / "facts.toml"
    path.write_text(FACTS)
    import_facts(engine, read_facts(path))
    retire_fact(engine, "project-old")
    return engine


CLAIMS = [
    # Supported: accepted
    Claim("Cut inference cost by 35% at Acme", ("role-acme",)),
    Claim("Built durable agent workflows with LangGraph", ("skill-langgraph",)),
    Claim("Served 1200 customers between 2021 and 2024", ("role-acme",)),
    Claim("Used Kubernetes to deploy model services", ("skill-k8s",)),
    Claim("Built a FastAPI gateway", ("project-inferex",)),  # FastAPI is "built"
    # Unsupported: blocked, with the reason
    Claim("Led the ML platform team at Acme", ()),
    Claim("Expert in TensorFlow", ("skill-tensorflow",)),
    Claim("Wrote services in Rust", ("skill-rust",)),
    Claim("Built a recommendation engine", ("project-old",)),
    Claim("Cut inference cost by 40% at Acme", ("role-acme",)),
    Claim("Served thousands of customers at Acme", ("role-acme",)),
    Claim("Cut LangGraph inference cost by 35%", ("skill-langgraph",)),  # 35% is another fact's
    Claim("Ran Kubernetes clusters in production", ("skill-k8s",)),
    Claim("Built pipelines with LangGraph and Kubernetes", ("skill-langgraph", "skill-k8s")),
    # A skill's level can't be borrowed by citing another fact: mentioned is enough.
    Claim("Deployed and operated Kubernetes in production", ("project-inferex",)),
    # A skill the bank holds only as a draft can't be named, cited or not.
    Claim("Added a Rust client to Inferex", ("project-inferex",)),
]
BLOCKED_FOR = {
    "Led the ML platform team at Acme": "cites no fact",
    "Expert in TensorFlow": "unknown fact 'skill-tensorflow'",
    "Wrote services in Rust": "'skill-rust' is draft, not verified",
    "Built a recommendation engine": "'project-old' is retired, not verified",
    "Cut inference cost by 40% at Acme": "number '40%' is not in its cited facts",
    "Served thousands of customers at Acme": "'thousands' is not in its cited facts",
    "Cut LangGraph inference cost by 35%": "number '35%' is not in its cited facts",
    "Ran Kubernetes clusters in production": "states production, above 'skill-k8s' (used)",
    "Built pipelines with LangGraph and Kubernetes": "states built, above 'skill-k8s' (used)",
    "Deployed and operated Kubernetes in production": "states production, above 'skill-k8s' (used)",
    "Added a Rust client to Inferex": "mentions unverified skill 'skill-rust' (draft)",
}


def test_unsupported_claims_are_blocked_never_reworded(bank: Engine) -> None:
    checked = check_claims(CLAIMS, latest_facts(bank), load_claim_levels(REPO_CONFIG))

    # Every claim comes back, in order, its text exactly as drafted.
    assert [c.text for c in checked.claims] == [c.text for c in CLAIMS]
    assert [c.text for c in checked.accepted] == [c.text for c in CLAIMS[:5]]
    assert {c.text for c in checked.blocked} == set(BLOCKED_FOR)
    for claim in checked.blocked:
        reason = BLOCKED_FOR[claim.text]
        assert any(reason in p for p in claim.problems), (claim.text, claim.problems)


def test_an_accepted_claim_keeps_the_versions_it_was_checked_against(bank: Engine) -> None:
    facts = latest_facts(bank)
    [checked] = check_claims(CLAIMS[:1], facts, load_claim_levels(REPO_CONFIG)).claims
    [role] = [f for f in facts if f.fact_id == "role-acme"]
    assert (checked.blocked, checked.versions) == (False, (role.id,))


def test_every_reason_is_listed(bank: Engine) -> None:
    # No fact at all, so its number can't be in a cited fact either.
    claim = Claim("Grew revenue 3x in 2 years", ())
    [checked] = check_claims([claim], latest_facts(bank), load_claim_levels(REPO_CONFIG)).claims
    assert checked.text == claim.text
    assert len(checked.problems) == 3  # no fact; '3x'; '2'


def test_an_empty_draft_has_nothing_to_block(bank: Engine) -> None:
    checked = check_claims([], latest_facts(bank), load_claim_levels(REPO_CONFIG))
    assert (checked.claims, checked.blocked) == ([], [])


def test_a_drafted_pack_stores_unsupported_claims_as_blocked_and_unchanged(bank: Engine) -> None:
    # The drafting path (ADR 0016): what a model drafts is checked before it
    # is stored, and a blocked claim is kept exactly as the model wrote it.
    drafted = {
        "cv_lines": [
            {"text": "Cut inference cost by 35% at Acme", "fact_ids": ["role-acme"]},
            {"text": "Cut inference cost by over 40% at Acme", "fact_ids": ["role-acme"]},
            {"text": "Led a team of 12 at Acme", "fact_ids": []},
        ],
        "cover_note": [
            {"text": "I ran Kubernetes in production at Acme", "fact_ids": ["role-acme"]},
        ],
    }

    def model(request: ModelRequest) -> ModelResponse:
        record = CallRecord("anthropic", "claude-sonnet-5-5", request.step, 1, 0, 0, 1, 1, None)
        return ModelResponse(text=json.dumps(drafted), stop="end", record=record)

    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        bank, url="https://x/1", company="Fin", title="AI Engineer", dedupe_key="f" * 64,
        snapshot=refs,
    )  # fmt: skip
    with bank.connect() as conn:
        snapshot_id = conn.execute(text("SELECT snapshot_id FROM applications")).scalar_one()
    draft = draft_pack(model, latest_facts(bank), "A job.", load_claim_levels(REPO_CONFIG))
    store_draft(bank, draft, application_id=application_id, thread_id="pack-g-1",
                snapshot_id=snapshot_id)  # fmt: skip

    with bank.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT section, text, blocked FROM draft_claims WHERE round = 0"
                " ORDER BY section, position"
            )
        ).all()
    assert [(c.section, c.text, c.blocked) for c in stored] == [
        ("cover_note", "I ran Kubernetes in production at Acme", True),
        ("cv_line", "Cut inference cost by 35% at Acme", False),
        ("cv_line", "Cut inference cost by over 40% at Acme", True),
        ("cv_line", "Led a team of 12 at Acme", True),
    ]
