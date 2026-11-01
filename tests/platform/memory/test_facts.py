"""The fact bank (ADR 0014): the owner's TOML file, validated all-or-nothing;
imports that are safe to re-run; versions that never change; and only the
latest verified version of a fact is citable."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.platform.memory.facts import (
    FactsError,
    citable_facts,
    import_facts,
    latest_facts,
    plan_import,
    read_facts,
    retire_fact,
)

ROLE = """
[[fact]]
id = "role-acme-2021"
kind = "role"
text = "ML engineer at Acme, 2021 to 2024"
evidence = "https://example.com/acme-reference.pdf"
verified_at = 2026-10-30
status = "verified"
"""
SKILL = """
[[fact]]
id = "skill-langgraph"
kind = "skill"
text = "LangGraph"
evidence = "github.com/me/jhunt"
verified_at = "2026-10-31"
status = "verified"
level = "built"
"""
DRAFT = """
[[fact]]
id = "skill-k8s"
kind = "skill"
text = "Kubernetes"
status = "draft"
level = "TODO"
"""


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "facts.toml"
    path.write_text(body)
    return path


def test_a_good_file_reads_every_field(tmp_path: Path) -> None:
    role, skill, draft = read_facts(write(tmp_path, ROLE + SKILL + DRAFT))
    assert (role.fact_id, role.kind, role.status, role.verified_at, role.level) == (
        "role-acme-2021", "role", "verified", date(2026, 10, 30), None,
    )  # fmt: skip
    assert (skill.verified_at, skill.level, skill.evidence) == (
        date(2026, 10, 31), "built", "github.com/me/jhunt",
    )  # fmt: skip
    # A draft may be incomplete: "TODO" is no level, and there's no evidence.
    assert (draft.status, draft.level, draft.evidence, draft.verified_at) == (
        "draft", None, None, None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("[[fact]\n", "invalid TOML"),
        (ROLE.replace("[[fact]]", "[[facts]]"), "write [[fact]]"),
        (ROLE + "[other]\nx = 1\n", "only [[fact]] tables"),
        (ROLE.replace('id = "role-acme-2021"\n', ""), "fact 1: id is required"),
        (ROLE.replace('id = "role-acme-2021"', 'id = "has space"'), "fact 1: id 'has space'"),
        (ROLE.replace('text = "ML engineer at Acme, 2021 to 2024"', 'text = " "'),
         "role-acme-2021: text is required"),
        (ROLE.replace('kind = "role"', 'kind = "hobby"'), "role-acme-2021: kind 'hobby'"),
        (ROLE.replace('status = "verified"', 'status = "retired"'), "status 'retired'"),
        (ROLE + ROLE, "role-acme-2021: the id is used twice"),
        (ROLE + 'notes = "x"\n', "role-acme-2021: unknown field 'notes'"),
        # Verified facts are strict
        (ROLE.replace('evidence = "https://example.com/acme-reference.pdf"\n', ""),
         "role-acme-2021: a verified fact needs evidence"),
        (ROLE.replace("verified_at = 2026-10-30\n", ""),
         "role-acme-2021: a verified fact needs verified_at"),
        (SKILL.replace('level = "built"\n', ""),
         "skill-langgraph: a verified skill needs a level"),
        (SKILL.replace('level = "built"', 'level = "TODO"'),
         "skill-langgraph: a verified skill needs a level"),
        (SKILL.replace('level = "built"', 'level = "expert"'), "level 'expert'"),
        (ROLE + 'level = "built"\n', "role-acme-2021: level is only for skills"),
        (ROLE.replace("verified_at = 2026-10-30", 'verified_at = "last week"'),
         "verified_at 'last week'"),
        (ROLE.replace("verified_at = 2026-10-30", "verified_at = 2999-01-01"), "in the future"),
        # A draft can be incomplete, but not malformed
        (DRAFT.replace('level = "TODO"', 'level = "expert"'), "skill-k8s: level 'expert'"),
        (DRAFT.replace('kind = "skill"', 'kind = "role"'), "skill-k8s: level is only for skills"),
    ],
)  # fmt: skip
def test_a_bad_fact_rejects_the_whole_file(tmp_path: Path, body: str, problem: str) -> None:
    with pytest.raises(FactsError) as raised:
        read_facts(write(tmp_path, body))
    assert any(problem in p for p in raised.value.problems), raised.value.problems


def test_every_problem_is_listed_at_once(tmp_path: Path) -> None:
    body = ROLE.replace('kind = "role"', 'kind = "hobby"') + SKILL.replace('level = "built"\n', "")
    with pytest.raises(FactsError) as raised:
        read_facts(write(tmp_path, body))
    assert len(raised.value.problems) == 2


def versions(engine: Engine) -> list[tuple[str, int, str]]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT fact_id, version, status FROM facts ORDER BY fact_id, version")
            )
        ]


def test_importing_again_writes_only_what_changed(engine: Engine, tmp_path: Path) -> None:
    first = import_facts(engine, read_facts(write(tmp_path, ROLE + SKILL + DRAFT)))
    assert [f.fact_id for f in first.new] == ["role-acme-2021", "skill-langgraph", "skill-k8s"]
    assert versions(engine) == [
        ("role-acme-2021", 1, "verified"),
        ("skill-k8s", 1, "draft"),
        ("skill-langgraph", 1, "verified"),
    ]

    # The same file again: nothing written.
    again = import_facts(engine, read_facts(write(tmp_path, ROLE + SKILL + DRAFT)))
    assert (again.new, again.changed) == ([], [])
    assert sorted(again.unchanged) == ["role-acme-2021", "skill-k8s", "skill-langgraph"]
    assert len(versions(engine)) == 3

    # One fact changed, one left out of the file.
    edited = SKILL.replace('level = "built"', 'level = "production"')
    plan = import_facts(engine, read_facts(write(tmp_path, ROLE + edited)))
    assert [f.fact_id for f in plan.changed] == ["skill-langgraph"]
    assert plan.unchanged == ["role-acme-2021"]
    assert plan.missing == ["skill-k8s"]  # listed, never retired by an import
    assert versions(engine) == [
        ("role-acme-2021", 1, "verified"),
        ("skill-k8s", 1, "draft"),
        ("skill-langgraph", 1, "verified"),
        ("skill-langgraph", 2, "verified"),
    ]


def test_a_plan_writes_nothing(engine: Engine, tmp_path: Path) -> None:
    plan = plan_import(engine, read_facts(write(tmp_path, ROLE)))
    assert [f.fact_id for f in plan.new] == ["role-acme-2021"]
    assert versions(engine) == []


def test_retire_adds_a_version_and_import_never_brings_it_back(
    engine: Engine, tmp_path: Path
) -> None:
    import_facts(engine, read_facts(write(tmp_path, ROLE + SKILL)))
    assert retire_fact(engine, "role-acme-2021") == 2
    with pytest.raises(FactsError, match="already retired"):
        retire_fact(engine, "role-acme-2021")
    with pytest.raises(FactsError, match="no fact 'nope'"):
        retire_fact(engine, "nope")

    # Still in the file, even edited: it stays retired.
    plan = import_facts(engine, read_facts(write(tmp_path, ROLE.replace("2024", "2025") + SKILL)))
    assert plan.retired_in_file == ["role-acme-2021"]
    assert (plan.new, plan.changed) == ([], [])
    assert versions(engine)[:2] == [
        ("role-acme-2021", 1, "verified"),
        ("role-acme-2021", 2, "retired"),
    ]
    [retired] = [f for f in latest_facts(engine) if f.fact_id == "role-acme-2021"]
    assert (retired.status, retired.text) == ("retired", "ML engineer at Acme, 2021 to 2024")


def test_only_the_latest_verified_version_is_citable(engine: Engine, tmp_path: Path) -> None:
    import_facts(engine, read_facts(write(tmp_path, ROLE + SKILL + DRAFT)))
    import_facts(
        engine,
        read_facts(write(tmp_path, ROLE + SKILL.replace("LangGraph", "LangGraph 1.x") + DRAFT)),
    )
    retire_fact(engine, "role-acme-2021")

    citable = citable_facts(engine)
    # Not the draft, not the retired role, and the skill only at version 2.
    assert [(f.fact_id, f.version, f.text) for f in citable] == [
        ("skill-langgraph", 2, "LangGraph 1.x"),
    ]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE facts SET text = 'better'", "DELETE FROM facts", "TRUNCATE facts"],
)
def test_fact_versions_never_change(engine: Engine, tmp_path: Path, statement: str) -> None:
    import_facts(engine, read_facts(write(tmp_path, ROLE)))
    with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
        conn.execute(text(statement))
    assert versions(engine) == [("role-acme-2021", 1, "verified")]
