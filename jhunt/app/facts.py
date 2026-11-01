"""`jhunt-facts import FILE | retire ID | list`: the owner's fact bank (ADR 0014).

import  reads your facts file (TOML, one [[fact]] table per fact, kept outside
        git), checks all of it, shows what's new, changed, unchanged and left
        out, and writes the new versions once you type yes. Safe to re-run.
retire  adds a retired version of a fact, after you type its id.
list    shows each fact's latest version and how many are citable.
"""

import argparse
from pathlib import Path
from typing import NoReturn

from sqlalchemy import Engine, create_engine

from jhunt.platform.config import get_settings
from jhunt.platform.memory.facts import (
    FactsError,
    citable_facts,
    import_facts,
    latest_facts,
    plan_import,
    read_facts,
    retire_fact,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-facts", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import", help="import your facts file").add_argument("file")
    commands.add_parser("retire", help="retire a fact").add_argument("fact_id")
    commands.add_parser("list", help="each fact's latest version")
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url())
    try:
        if args.command == "import":
            _import(engine, Path(args.file).expanduser())
        elif args.command == "retire":
            _retire(engine, args.fact_id)
        else:
            _list(engine)
    except FactsError as exc:
        for problem in exc.problems:
            print(f"  {problem}")
        _fail(exc.problems[0] if len(exc.problems) == 1 else f"{len(exc.problems)} problems")


def _import(engine: Engine, path: Path) -> None:
    repo = _git_repository(path)
    if repo is not None:
        _fail(f"{path} is inside a git repository ({repo}); facts never go in git")
    if not path.is_file():
        _fail(f"no such file: {path}")
    try:
        facts = read_facts(path)
    except FactsError as exc:
        for problem in exc.problems:
            print(f"  {problem}")
        _fail(
            f"{len(exc.problems)} problem{'' if len(exc.problems) == 1 else 's'}; nothing written"
        )

    plan = plan_import(engine, facts)
    print(f"{path}: {len(facts)} facts")
    _show("new", [f.fact_id for f in plan.new])
    _show("changed", [f.fact_id for f in plan.changed])
    _show("not in the file, kept as they are", plan.missing)
    _show("retired, still in the file, kept retired", plan.retired_in_file)
    if plan.missing:
        print("  (to retire a fact: jhunt-facts retire <id>)")
    writes = len(plan.new) + len(plan.changed)
    if writes == 0:
        print(f"nothing to import: {len(plan.unchanged)} unchanged")
        return
    print(f"  unchanged: {len(plan.unchanged)}")
    print("Each version is kept for good: an edit later is a new version, never a change.")
    if input(f"Type yes to write {writes} fact versions: ").strip() != "yes":
        _fail("not confirmed; nothing written")
    done = import_facts(engine, facts)  # planned again under the lock
    print(f"wrote {len(done.new) + len(done.changed)} fact versions")


def _retire(engine: Engine, fact_id: str) -> None:
    # Checked before asking; retire_fact checks again under its lock.
    latest = {f.fact_id: f for f in latest_facts(engine)}.get(fact_id)
    if latest is None:
        _fail(f"no fact {fact_id!r}")
    if latest.status == "retired":
        _fail(f"{fact_id} is already retired (version {latest.version})")
    print(f"{fact_id}  v{latest.version}  {latest.status}  {latest.kind}  {latest.text}")
    if input(f"Type {fact_id} to retire it: ").strip() != fact_id:
        _fail("not confirmed; nothing retired")
    version = retire_fact(engine, fact_id)
    print(f"retired: {fact_id} (version {version})")


def _list(engine: Engine) -> None:
    latest = latest_facts(engine)
    for f in latest:
        level = f" ({f.level})" if f.level else ""
        print(f"{f.fact_id}  v{f.version}  {f.status}  {f.kind}{level}  {f.text[:60]}")
    print(f"citable: {len(citable_facts(engine))} of {len(latest)}")


def _show(label: str, ids: list[str]) -> None:
    if ids:
        print(f"  {label} ({len(ids)}): {', '.join(ids)}")


def _git_repository(path: Path) -> Path | None:
    """The git work tree the file is in, if any: a .git above it."""
    for folder in path.resolve().parents:
        if (folder / ".git").exists():
            return folder
    return None


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"jhunt-facts: {message}")
