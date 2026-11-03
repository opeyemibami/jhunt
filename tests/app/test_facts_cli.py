"""jhunt-facts: import (safe to re-run, confirmed with yes), retire, list and
export (ADR 0014, ADR 0017). Facts files inside a git repository are refused.
The database is the source of truth: import refuses a file that would undo
edits made in the browser since it was exported, unless --overwrite."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.app import facts as cmd
from jhunt.platform.memory.facts import (
    FactsError,
    import_facts,
    latest_facts,
    read_facts,
    save_fact,
)

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


TRICKY = r"""
[[fact]]
id = "skill-k8s"
kind = "skill"
text = "Kubernetes \\ \"k8s\"\nand Helm, café\u007f"
evidence = "course"
verified_at = 2026-10-30
status = "verified"
level = "used"
aliases = ["Kubernetes", "k8s"]
"""


def test_export_writes_every_live_fact_back_as_a_facts_file(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    facts_file.write_text(ROLE + TRICKY)
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    save_fact(engine, "role-acme-2021", {"text": "ML engineer at Acme, 2021 to 2025"}, version=1)
    exported = facts_file.parent / "exported.toml"

    out = run(monkeypatch, capsys, "export", str(exported))
    assert f"wrote 2 facts to {exported}" in out
    # The same facts, as their latest versions, in a file import reads.
    assert sorted(f.fact_id for f in read_facts(exported)) == ["role-acme-2021", "skill-k8s"]
    by_id = {f.fact_id: f for f in read_facts(exported)}
    assert by_id["role-acme-2021"].text == "ML engineer at Acme, 2021 to 2025"
    assert by_id["skill-k8s"].text == 'Kubernetes \\ "k8s"\nand Helm, café\x7f'
    assert by_id["skill-k8s"].aliases == ("Kubernetes", "k8s")
    assert exported.stat().st_mode & 0o077 == 0  # yours only
    # Importing it straight back writes nothing.
    assert "nothing to import: 2 unchanged" in run(monkeypatch, capsys, "import", str(exported))


def test_export_leaves_retired_facts_out(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    run(monkeypatch, capsys, "retire", "skill-k8s", typed="skill-k8s")
    exported = facts_file.parent / "exported.toml"
    run(monkeypatch, capsys, "export", str(exported))
    assert [f.fact_id for f in read_facts(exported)] == ["role-acme-2021"]
    out = run(monkeypatch, capsys, "import", str(exported))
    assert "nothing to import: 1 unchanged" in out and "skill-k8s" not in out


def test_export_asks_before_replacing_a_file_and_refuses_git(
    engine: Engine, facts_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    before = facts_file.read_text()
    with pytest.raises(SystemExit, match="not confirmed; nothing written"):
        run(monkeypatch, capsys, "export", str(facts_file), typed="no")
    assert facts_file.read_text() == before
    run(monkeypatch, capsys, "export", str(facts_file), typed="yes")
    assert "exported_through" in facts_file.read_text()

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit, match="inside a git repository"):
        run(monkeypatch, capsys, "export", str(repo / "facts.toml"))
    assert not (repo / "facts.toml").exists()


def test_import_refuses_to_undo_browser_edits_made_since_the_export(
    engine: Engine, facts_file: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run(monkeypatch, capsys, "import", str(facts_file), typed="yes")
    save_fact(engine, "role-acme-2021", {"text": "ML engineer at Acme, 2021 to 2025"}, version=1)

    # The original file has no export mark: any browser edit is newer.
    with pytest.raises(SystemExit, match="edited in the browser since this file was exported"):
        run(monkeypatch, capsys, "import", str(facts_file))
    assert "role-acme-2021" in capsys.readouterr().out
    assert count(engine) == 3

    # Exported after the edit: the file has it, so importing is fine.
    exported = facts_file.parent / "exported.toml"
    run(monkeypatch, capsys, "export", str(exported))
    exported.write_text(exported.read_text().replace('text = "Kubernetes"', 'text = "Helm"'))
    out = run(monkeypatch, capsys, "import", str(exported), typed="yes")
    assert "changed (1): skill-k8s" in out

    # Edited in the browser again: the export is out of date.
    save_fact(engine, "skill-k8s", {"text": "Kubernetes and Helm"}, version=2)
    with pytest.raises(SystemExit, match="since this file was exported"):
        run(monkeypatch, capsys, "import", str(exported))

    # --overwrite: the file wins, as new versions; the browser's are kept in history.
    out = run(monkeypatch, capsys, "import", "--overwrite", str(exported), typed="yes")
    assert "changed (1): skill-k8s" in out
    assert {f.fact_id: f.text for f in latest_facts(engine)}["skill-k8s"] == "Helm"


def test_the_import_itself_refuses_under_its_lock(engine: Engine, facts_file: Path) -> None:
    # As when a browser edit lands between the command's check and its write.
    import_facts(engine, read_facts(facts_file))
    save_fact(engine, "skill-k8s", {"text": "Kubernetes and Helm"}, version=1)
    with pytest.raises(FactsError, match="skill-k8s: edited in the browser"):
        import_facts(engine, read_facts(facts_file))
    assert count(engine) == 3
    import_facts(engine, read_facts(facts_file), overwrite=True)
    assert count(engine) == 4
