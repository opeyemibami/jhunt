"""jhunt-facts: import (safe to re-run, confirmed with yes), retire and list
(ADR 0014). Facts files inside a git repository are refused."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.app import facts as cmd

ROLE = """
[[fact]]
id = "role-acme-2021"
kind = "role"
text = "ML engineer at Acme, 2021 to 2024"
evidence = "reference letter"
verified_at = 2026-10-30
status = "verified"
"""
DRAFT = """
[[fact]]
id = "skill-k8s"
kind = "skill"
text = "Kubernetes"
status = "draft"
"""


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, migrated_db_url: URL) -> Iterator[Engine]:
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    cmd.get_settings.cache_clear()
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()
    cmd.get_settings.cache_clear()


@pytest.fixture
def facts_file(tmp_path: Path) -> Path:
    path = tmp_path / "private" / "facts.toml"  # not in a git repository
    path.parent.mkdir()
    path.write_text(ROLE + DRAFT)
    return path


def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str,
        typed: str | None = None) -> str:  # fmt: skip
    def answer(prompt: str) -> str:
        if typed is None:
            pytest.fail(f"unexpected prompt: {prompt}")
        return typed

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(sys, "argv", ["jhunt-facts", *args])
    cmd.main()
    return capsys.readouterr().out


def count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM facts")).scalar_one())


def test_import_shows_the_plan_and_writes_only_after_yes(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    with pytest.raises(SystemExit, match="not confirmed; nothing written"):
        run(monkeypatch, capsys, "import", str(facts_file), typed="no")
    assert count(engine) == 0

    out = run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    assert "new (2): role-acme-2021, skill-k8s" in out
    assert "wrote 2 fact versions" in out
    assert count(engine) == 2

    # Again: nothing to write, so no prompt.
    out = run(monkeypatch, capsys, "import", str(facts_file))
    assert "nothing to import: 2 unchanged" in out
    assert count(engine) == 2


def test_import_lists_facts_left_out_and_changes_nothing_for_them(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    facts_file.write_text(ROLE.replace("2024", "2025"))
    out = run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    assert "changed (1): role-acme-2021" in out
    assert "not in the file, kept as they are (1): skill-k8s" in out
    assert "jhunt-facts retire" in out
    assert count(engine) == 3


def test_a_bad_file_lists_every_problem_and_writes_nothing(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    facts_file.write_text(ROLE.replace('kind = "role"', 'kind = "hobby"') + DRAFT + 'level = "x"\n')
    with pytest.raises(SystemExit, match="2 problems; nothing written"):
        run(monkeypatch, capsys, "import", str(facts_file))
    out = capsys.readouterr().out
    assert "kind 'hobby'" in out and "level 'x'" in out
    assert count(engine) == 0


def test_a_file_inside_a_git_repository_is_refused(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "notes").mkdir()
    inside = repo / "notes" / "facts.toml"
    inside.write_text(ROLE)
    with pytest.raises(SystemExit, match=r"inside a git repository .*; facts never go in git"):
        run(monkeypatch, capsys, "import", str(inside))
    assert count(engine) == 0


def test_retire_and_list(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    with pytest.raises(SystemExit, match="not confirmed"):
        run(monkeypatch, capsys, "retire", "role-acme-2021", typed="no")
    out = run(monkeypatch, capsys, "retire", "role-acme-2021", typed="role-acme-2021")
    assert "retired: role-acme-2021 (version 2)" in out

    out = run(monkeypatch, capsys, "list")
    assert "role-acme-2021  v2  retired  role" in out
    assert "skill-k8s  v1  draft  skill" in out
    assert "citable: 0 of 2" in out

    with pytest.raises(SystemExit, match="jhunt-facts: no fact 'nope'"):
        run(monkeypatch, capsys, "retire", "nope")
