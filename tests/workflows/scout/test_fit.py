"""The fit score: deterministic, from config/targets.toml (ADR 0011)."""

from pathlib import Path

import pytest

from jhunt.workflows.scout.fit import Targets, TargetsError, fit_score, load_targets

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"

TARGETS = Targets(
    title_weight=0.6,
    title_phrases=["ml engineer", "software engineer, ai", "platform engineer", "agent"],
    include_weight=0.08,
    include_max=0.4,
    include_keywords=[
        "llm",
        "agents",
        "python",
        "kubernetes",
        "rag",
        "gcp",
        "computer vision",
        "eval",
    ],
    exclude_keywords=["manager", "intern", "head of"],
    penalty_weight=0.3,
    penalty_keywords=["staff", "principal"],
)


@pytest.mark.parametrize(
    ("title", "description", "score", "matches"),
    [
        # A title phrase plus keywords from the title or description
        (
            "ML Engineer",
            "<p>Ship LLM agents in Python.</p>",
            0.84,
            {"title": ["ml engineer"], "include": ["llm", "agents", "python"]},
        ),
        ("Platform Engineer", "", 0.6, {"title": ["platform engineer"]}),
        ("Software Engineer, AI", "", 0.6, {"title": ["software engineer, ai"]}),
        # Include keywords alone, and their cap
        ("Data Engineer", "<p>Python on GCP.</p>", 0.16, {"include": ["python", "gcp"]}),
        (
            "ML Engineer",
            "LLM agents, Python, Kubernetes, RAG, GCP and computer vision",
            1.0,  # 0.6 + 0.4 (seven keywords, capped at 0.4)
            {
                "title": ["ml engineer"],
                "include": [
                    "llm", "agents", "python", "kubernetes", "rag", "gcp", "computer vision"
                ],
            },
        ),
        (
            "Data Engineer",
            "LLM agents, Python, Kubernetes, RAG, GCP and computer vision",
            0.4,  # seven keywords, capped at 0.4, with no title phrase
            {"include": ["llm", "agents", "python", "kubernetes", "rag", "gcp", "computer vision"]},
        ),
        # Penalty: title only, once, lowers without hiding; clamped at 0
        (
            "Principal Staff ML Engineer", "", 0.3,
            {"title": ["ml engineer"], "penalty": ["staff", "principal"]},
        ),
        ("Staff ML Engineer", "", 0.3, {"title": ["ml engineer"], "penalty": ["staff"]}),
        ("Principal Designer", "", 0.0, {"penalty": ["principal"]}),
        ("Designer", "<p>Work with staff across the company.</p>", 0.0, {}),
        # Exclude: title only, and the score is 0 whatever else matches
        ("Engineering Manager, ML Engineer team", "LLM", 0.0, {"exclude": ["manager"]}),
        ("Head of Platform Engineer hiring", "", 0.0, {"exclude": ["head of"]}),
        ("ML Engineer", "<p>Reports to a manager.</p>", 0.6, {"title": ["ml engineer"]}),
        # A trailing "s" matches too: plurals
        # "agents" is also an include keyword, found in the title
        ("AI Agents Engineer", "", 0.68, {"title": ["agent"], "include": ["agents"]}),
        ("ML Engineers", "", 0.6, {"title": ["ml engineer"]}),
        ("Data Engineer", "<p>Ship LLMs with evals.</p>", 0.16, {"include": ["llm", "eval"]}),
        ("Interns Programme Coordinator", "", 0.0, {"exclude": ["intern"]}),
        # Whole words only, plurals aside
        ("Internal Tools Engineer", "", 0.0, {}),  # not "intern"
        ("International Partnerships Engineer", "", 0.0, {}),  # not "intern"
        ("Agentic Systems Engineer", "", 0.0, {}),  # not "agent"
        ("HTML Engineer", "<p>Ragged edges, pythonic.</p>", 0.0, {}),  # not ml, rag, python
    ],
)  # fmt: skip
def test_fit_score(
    title: str, description: str, score: float, matches: dict[str, list[str]]
) -> None:
    assert fit_score(title, description, TARGETS) == (pytest.approx(score), matches)


def test_repo_targets_are_the_approved_ones() -> None:
    targets = load_targets(REPO_CONFIG)
    assert (targets.title_weight, len(targets.title_phrases)) == (0.5, 21)
    assert (targets.include_weight, targets.include_max, len(targets.include_keywords)) == (
        0.08,
        0.5,
        24,
    )
    assert len(targets.exclude_keywords) == 18
    assert (targets.penalty_weight, targets.penalty_keywords) == (
        0.3,
        [
            "staff engineer",
            "staff software engineer",
            "staff ml",
            "staff machine learning",
            "staff+",
            "principal",
            "distinguished",
        ],
    )


# The owner's tuning after the first real scores (2026-11-01), on the repo's own
# targets.toml: "staff" alone is no longer a penalty, title matches no longer
# tie at 1.0, and three more exclusions.
@pytest.mark.parametrize(
    ("title", "description", "score", "matches"),
    [
        ("Forward Deployed Engineer", "", 0.5, {"title": ["forward deployed"]}),
        # "Member of Technical Staff" is a normal title (Cohere, Anthropic)
        ("Member of Technical Staff", "", 0.0, {}),
        (
            "Member of Technical Staff, Inference",
            "",
            0.58,
            {"title": ["inference"], "include": ["inference"]},
        ),
        # Staff as a level is still penalised
        ("Staff Software Engineer, ML Platform", "", 0.2, {
            "title": ["ml platform", "software engineer, ml"],
            "penalty": ["staff software engineer"]}),
        ("Staff ML Engineer", "", 0.2, {
            "title": ["ml engineer"], "penalty": ["staff ml"]}),
        ("Staff Machine Learning Engineer", "", 0.2, {
            "title": ["machine learning engineer"], "penalty": ["staff machine learning"]}),
        # "+" isn't whitespace, so only "staff+" matches; the penalty is once anyway
        ("Staff+ Engineer, Agents", "", 0.28, {
            "title": ["agent"], "include": ["agents"], "penalty": ["staff+"]}),
        # A title match plus the most include keywords: 0.5 + 0.5, the only way to 1.0
        ("ML Engineer", "LLM agents, RAG, evals, inference, serving, Python", 1.0, {
            "title": ["ml engineer"],
            "include": ["llm", "agents", "rag", "evals", "inference", "serving", "python"]}),
        ("ML Engineer", "LLM agents and RAG in Python", 0.82, {
            "title": ["ml engineer"], "include": ["llm", "agents", "rag", "python"]}),
        # The three new exclusions
        ("Business Development Representative, AI", "", 0.0, {
            "exclude": ["business development", "representative"]}),
        ("Customer Success Representative", "LLM", 0.0, {"exclude": ["representative"]}),
        ("IT Support Engineer", "", 0.0, {"exclude": ["it support"]}),
    ],
)  # fmt: skip
def test_the_repo_targets_score_these_titles(
    title: str, description: str, score: float, matches: dict[str, list[str]]
) -> None:
    assert fit_score(title, description, load_targets(REPO_CONFIG)) == (
        pytest.approx(score),
        matches,
    )


GOOD = """
[title]
weight = 0.6
phrases = ["ml engineer"]
[include]
weight = 0.1
max = 0.4
keywords = ["python"]
[exclude]
keywords = ["manager"]
[penalty]
weight = 0.3
keywords = ["staff"]
"""


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (GOOD.replace('[penalty]\nweight = 0.3\nkeywords = ["staff"]\n', ""), "sections"),
        (GOOD + "[other]\n", "sections"),
        (GOOD.replace("weight = 0.6", "weight = 1.5"), "between 0 and 1"),
        (GOOD.replace("weight = 0.6", 'weight = "high"'), "between 0 and 1"),
        (GOOD.replace('phrases = ["ml engineer"]', 'phrases = ["ml engineer", ""]'), "non-empty"),
        (GOOD.replace('phrases = ["ml engineer"]', "phrases = []"), "non-empty"),
        (GOOD.replace("max = 0.4\n", ""), "max"),
    ],
)
def test_bad_targets_fail(tmp_path: Path, body: str, error: str) -> None:
    (tmp_path / "targets.toml").write_text(body)
    with pytest.raises(TargetsError, match=error):
        load_targets(tmp_path)


def test_good_targets_load(tmp_path: Path) -> None:
    (tmp_path / "targets.toml").write_text(GOOD)
    assert load_targets(tmp_path).include_max == 0.4


def test_missing_targets_file_fails(tmp_path: Path) -> None:
    with pytest.raises(TargetsError, match="targets file missing"):
        load_targets(tmp_path)
