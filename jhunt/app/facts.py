"""`jhunt-facts import FILE | export FILE | retire ID | list`: the owner's fact bank (ADR 0014).

import  reads your facts file (TOML, one [[fact]] table per fact, kept outside
        git), checks all of it, shows what's new, changed, unchanged and left
        out, and writes the new versions once you type yes. Safe to re-run.
        The database is the source of truth (ADR 0017): it refuses a file that
        would undo an edit made on the Facts page since the file was exported,
        unless given --overwrite.
export  writes every fact that isn't retired to FILE (outside git), for you to
        edit and import again. Asks before replacing a file.
retire  adds a retired version of a fact, after you type its id.
list    shows each fact's latest version and how many are citable.
"""

import argparse
import os
from pathlib import Path
from typing import NoReturn

from sqlalchemy import Engine, create_engine

from jhunt.platform.config import get_settings
from jhunt.platform.memory.facts import (
    EDITED_SINCE_EXPORT,
    FactsError,
    browser_edits_since,
    citable_facts,
    export_facts,
    import_facts,
    latest_facts,
    plan_import,
    read_export_mark,
    read_facts,
    retire_fact,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-facts", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import", help="import your facts file")
    importing.add_argument("file")
    importing.add_argument(
        "--overwrite", action="store_true", help="let the file win over Facts page edits"
    )
    commands.add_parser("export", help="write your facts to a file").add_argument("file")
    commands.add_parser("retire", help="retire a fact").add_argument("fact_id")
    commands.add_parser("list", help="each fact's latest version")
    args = parser.parse_args()

    engine = create_engine(get_settings().database_url())
    try:
        if args.command == "import":
            _import(engine, Path(args.file).expanduser(), overwrite=args.overwrite)
        elif args.command == "export":
            _export(engine, Path(args.file).expanduser())
        elif args.command == "retire":
            _retire(engine, args.fact_id)
        else:
            _list(engine)
    except FactsError as exc:
        for problem in exc.problems:
            print(f"  {problem}")
        _fail(exc.problems[0] if len(exc.problems) == 1 else f"{len(exc.problems)} problems")


def _import(engine: Engine, path: Path, *, overwrite: bool) -> None:
    _outside_git(path)
    if not path.is_file():
        _fail(f"no such file: {path}")
    try:
        facts = read_facts(path)
        mark = read_export_mark(path)
    except FactsError as exc:
        for problem in exc.problems:
            print(f"  {problem}")
        _fail(
            f"{len(exc.problems)} problem{'' if len(exc.problems) == 1 else 's'}; nothing written"
        )

    # Checked before asking; import_facts checks again under its lock.
    edited = browser_edits_since(engine, mark)
    if edited and not overwrite:
        print(f"  edited on the Facts page: {', '.join(edited)}")
        _fail(EDITED_SINCE_EXPORT)
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
    # Planned again under the lock.
    done = import_facts(engine, facts, exported_through=mark, overwrite=overwrite)
    print(f"wrote {len(done.new) + len(done.changed)} fact versions")


def _export(engine: Engine, path: Path) -> None:
    _outside_git(path)
    if path.exists() and input(f"{path} exists. Type yes to replace it: ").strip() != "yes":
        _fail("not confirmed; nothing written")
    body, count = export_facts(engine)
    # Yours only, written whole: a crash leaves the old file, not half a new one.
    partial = path.with_name(f".{path.name}.partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(body)
    partial.replace(path)
    print(f"wrote {count} facts to {path}")


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


def _outside_git(path: Path) -> None:
    repo = _git_repository(path)
    if repo is not None:
        _fail(f"{path} is inside a git repository ({repo}); facts never go in git")


def _git_repository(path: Path) -> Path | None:
    """The git work tree the file is in, if any: a .git above it."""
    for folder in path.resolve().parents:
        if (folder / ".git").exists():
            return folder
    return None


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"jhunt-facts: {message}")
