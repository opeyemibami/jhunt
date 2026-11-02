"""The claim validator's rules in detail (ADR 0015): how numbers and level
words are read, and config/claim_levels.toml."""

import re
from datetime import date
from pathlib import Path

import pytest

from jhunt.platform.memory.claims import (
    LEVELS,
    Claim,
    ClaimLevels,
    ClaimLevelsError,
    check_claims,
    load_claim_levels,
)
from jhunt.platform.memory.facts import FactVersion

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


def fact(fact_id: str, body: str, *, kind: str = "metric", level: str | None = None,
         status: str = "verified", row: int = 1,
         aliases: tuple[str, ...] = ()) -> FactVersion:  # fmt: skip
    return FactVersion(
        row, fact_id, 1, kind, body, "evidence", date(2026, 10, 30), status, level, aliases
    )


LEVEL_WORDS = ClaimLevels(
    {
        "studied": ("studied", "familiar with"),
        "used": ("used", "worked with"),
        "built": ("built",),
        "production": ("in production", "deployed"),
    }
)


def blocked(text: str, facts: list[FactVersion], cites: tuple[str, ...] = ("m",)) -> list[str]:
    [checked] = check_claims([Claim(text, cites)], facts, LEVEL_WORDS).claims
    return list(checked.problems)


@pytest.mark.parametrize(
    ("claim", "fact_text", "ok"),
    [
        ("Cut cost by 35%", "cut cost by 35%", True),
        ("Cut cost by 35 %", "cut cost by 35%", True),
        ("Cut cost by 36%", "cut cost by 35%", False),
        ("Cut cost by 35%", "35 engineers", False),  # the same number, another unit
        ("Served 1200 users", "served 1,200 users", True),
        ("Served 1,200 users", "served 1200 users", True),
        ("Grew 3x", "grew 3\u00d7", True),  # the times sign
        ("Raised 1.5M", "raised 1.5m", True),
        ("Raised 1.5M", "raised 1.5bn", False),
        ("Reached 10k users", "10k users", True),
        ("Reached 10k users", "10 users", False),
        ("From 2021 to 2024", "2021 to 2023", False),
        ("Sold B2B software on S3", "software", True),  # numbers inside words aren't numbers
        ("Served thousands of users", "users", False),
        ("Served thousands of users", "thousands of users", True),
        ("One of the first engineers", "an early engineer", False),
        ("Someone at Acme", "Acme", True),  # "one" only as a whole word
    ],
)
def test_numbers_must_be_in_the_cited_facts(claim: str, fact_text: str, ok: bool) -> None:
    problems = blocked(claim, [fact("m", fact_text)])
    assert (problems == []) is ok, problems


def test_a_number_in_another_fact_doesnt_count() -> None:
    facts = [fact("m", "cut cost by 35%"), fact("other", "grew 40%", row=2)]
    assert blocked("Grew 40%", facts) != []
    assert blocked("Grew 40%", facts, ("m", "other")) == []


@pytest.mark.parametrize(
    ("claim", "level", "ok"),
    [
        ("Used Kubernetes", "used", True),
        ("Familiar with Kubernetes", "used", True),
        ("Built tools on Kubernetes", "used", False),
        ("Deployed Kubernetes", "built", False),
        ("Ran Kubernetes IN PRODUCTION", "built", False),
        ("Deployed Kubernetes", "production", True),
        ("Used and deployed Kubernetes", "used", False),  # the highest level stated counts
        ("Kubernetes", "studied", True),  # no level words: no level stated
        ("Reused Kubernetes manifests", "studied", True),  # "used" only as a whole word
        ("Kubernetes deployment scripts", "studied", True),  # "deployment" isn't "deployed"
    ],
)
def test_a_skill_stated_above_its_level_is_blocked(claim: str, level: str, ok: bool) -> None:
    problems = blocked(claim, [fact("k8s", "Kubernetes", kind="skill", level=level)], ("k8s",))
    assert (problems == []) is ok, problems


def test_level_words_only_count_against_skill_facts() -> None:
    # "deployed" in a role claim is about the role, not a skill level.
    assert blocked("Deployed the platform", [fact("m", "deployed the platform", kind="role")]) == []


def test_the_repo_level_words_load_in_order() -> None:
    levels = load_claim_levels(REPO_CONFIG)
    assert list(levels.words) == list(LEVELS) == ["studied", "used", "built", "production"]
    assert all(levels.words[level] for level in LEVELS)


GOOD = """
[levels]
studied = ["studied"]
used = ["used"]
built = ["built"]
production = ["in production"]
"""


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (GOOD.replace('production = ["in production"]\n', ""), "exactly the levels"),
        (GOOD + 'expert = ["x"]\n', "exactly the levels"),
        (GOOD.replace('["built"]', "[]"), "built: a non-empty list"),
        (GOOD.replace('["built"]', '["used"]'), "'used' is listed under both"),
        ("[other]\n", "exactly one table, [levels]"),
    ],
)
def test_bad_level_words_fail(tmp_path: Path, body: str, error: str) -> None:
    (tmp_path / "claim_levels.toml").write_text(body)
    with pytest.raises(ClaimLevelsError, match=re.escape(error)):
        load_claim_levels(tmp_path)


def test_missing_level_words_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ClaimLevelsError, match="claim levels file missing"):
        load_claim_levels(tmp_path)


PROJECT = fact("project", "Inference platform: a FastAPI gateway on Kubernetes", kind="project")


@pytest.mark.parametrize(
    ("claim", "skill", "ok"),
    [
        # Mentioned but not cited: the level check still applies (owner, PR 3.6a review)
        ("Ran Kubernetes in production", fact("k8s", "Kubernetes", kind="skill", level="used",
         row=2, aliases=("Kubernetes", "k8s")), False),
        ("Ran k8s in production", fact("k8s", "Kubernetes", kind="skill", level="used",
         row=2, aliases=("Kubernetes", "k8s")), False),
        ("Built tools for K8S", fact("k8s", "Kubernetes", kind="skill", level="used",
         row=2, aliases=("Kubernetes", "k8s")), False),
        ("Built a FastAPI gateway", fact("api", "FastAPI", kind="skill", level="built",
         row=2), True),
        # No aliases: the skill's text, split on "and", ",", "/" and parentheses
        ("Deployed asyncio services", fact("py", "Python (FastAPI, asyncio)", kind="skill",
         level="built", row=2), False),
        ("Deployed TensorFlow models", fact("ml", "PyTorch and TensorFlow / JAX", kind="skill",
         level="used", row=2), False),
        ("Built JAX kernels", fact("ml", "PyTorch and TensorFlow / JAX", kind="skill",
         level="used", row=2), False),
        # Whole words only, and a claim that mentions no skill is unaffected
        ("Deployed Kubernetesque tooling", fact("k8s", "Kubernetes", kind="skill", level="used",
         row=2), True),
        ("Deployed the inference platform", fact("k8s", "Kubernetes", kind="skill", level="used",
         row=2), True),
        # Aliases replace the text: the text isn't matched when there are aliases
        ("Deployed Kubernetes", fact("k8s", "Kubernetes", kind="skill", level="used", row=2,
         aliases=("k8s",)), True),
    ],
)  # fmt: skip
def test_a_skill_mentioned_but_not_cited_is_checked_too(
    claim: str, skill: FactVersion, ok: bool
) -> None:
    problems = blocked(claim, [PROJECT, skill], ("project",))
    assert (problems == []) is ok, problems


@pytest.mark.parametrize("status", ["draft", "retired"])
def test_a_claim_mentioning_an_unverified_skill_is_blocked(status: str) -> None:
    # Owner, PR 3.6a review: naming a skill the bank doesn't vouch for is
    # adding a skill, whatever the wording.
    level = "used" if status == "retired" else None
    skill = fact("k8s", "Kubernetes", kind="skill", level=level, status=status, row=2)
    problems = blocked("Wrote Kubernetes operators", [PROJECT, skill], ("project",))
    assert problems == [f"mentions unverified skill 'k8s' ({status})"]


def test_citing_an_unverified_skill_is_reported_once() -> None:
    skill = fact("k8s", "Kubernetes", kind="skill", status="draft", row=2)
    assert blocked("Wrote Kubernetes operators", [skill], ("k8s",)) == [
        "fact 'k8s' is draft, not verified"
    ]
