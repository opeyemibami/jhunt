"""The fact bank: the owner's facts, one row per version (ADR 0014).

The owner keeps their facts in their own TOML file, one [[fact]] table each,
outside git. read_facts validates the whole file: one bad fact rejects all of
it, with every problem listed. import_facts writes what's new or changed as
new versions, skips what's unchanged, and only lists facts that are missing
from the file or retired. A row is never changed (migration 0010), so a draft
keeps citing the version it used.

Only a fact's latest version, and only when it is verified, is citable.

The database is the source of truth (ADR 0017). The Facts page saves an edit
with save_fact: the same checks as an import, then a new version made "via"
the browser. export_facts writes the facts back as a file, marked with the
newest row it holds (exported_through). import_facts refuses a file that
would undo a browser edit newer than its mark, unless told to overwrite.
"""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

KINDS = ("role", "project", "metric", "skill", "education")
LEVELS = ("studied", "used", "built", "production")
FIELDS = {"id", "kind", "text", "evidence", "verified_at", "status", "level", "aliases"}
# What the Facts page may change; kind, status and verified_at stay as they were.
EDITABLE = ("text", "evidence", "level", "aliases")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


class FactsError(Exception):
    """The facts file, or a request about a fact, can't be used. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class NoSuchFact(FactsError, LookupError):
    """There's no fact with that id."""


class FactChanged(FactsError):
    """The fact has a newer version than the one the edit started from."""


@dataclass(frozen=True)
class Fact:
    """A fact as the owner's file states it."""

    fact_id: str
    kind: str
    text: str
    status: str  # "draft" or "verified"
    evidence: str | None
    verified_at: date | None
    level: str | None  # skills only; None for a draft's "TODO"
    aliases: tuple[str, ...] = ()  # skills only: the names the skill goes by

    def content_sha256(self) -> str:
        return _content_sha256(
            self.kind, self.text, self.evidence, self.verified_at, self.status, self.level,
            self.aliases,
        )  # fmt: skip


@dataclass(frozen=True)
class FactVersion:
    """One stored version of a fact. id is what a draft cites."""

    id: int
    fact_id: str
    version: int
    kind: str
    text: str
    evidence: str | None
    verified_at: date | None
    status: str  # "draft", "verified" or "retired"
    level: str | None
    aliases: tuple[str, ...] = ()
    via: str = "command"  # "command" or "browser"


@dataclass
class ImportPlan:
    new: list[Fact] = field(default_factory=list)
    changed: list[Fact] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # imported before, not in the file
    retired_in_file: list[str] = field(default_factory=list)  # kept retired


def read_facts(path: Path) -> list[Fact]:
    """Every fact in the file, or FactsError listing every problem."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise FactsError([f"{path}: invalid TOML: {exc}"]) from None
    if "facts" in data and "fact" not in data:
        raise FactsError([f"{path}: write [[fact]] (singular) for each fact, not [[facts]]"])
    if set(data) - {"exported_through"} != {"fact"} or not isinstance(data["fact"], list):
        raise FactsError([f"{path}: must hold only [[fact]] tables"])
    export_mark(data, path)

    facts: list[Fact] = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, entry in enumerate(data["fact"], 1):
        found, wrong = _fact(entry, n)
        problems += wrong
        if found is None:
            continue
        if found.fact_id in seen:
            problems.append(f"{found.fact_id}: the id is used twice")
        seen.add(found.fact_id)
        facts.append(found)
    if problems:
        raise FactsError(problems)
    return facts


def read_export_mark(path: Path) -> int:
    """The newest facts row an exported file holds; 0 for a file never exported."""
    return export_mark(tomllib.loads(path.read_text(encoding="utf-8")), path)


def export_mark(data: dict[str, Any], path: Path) -> int:
    mark = data.get("exported_through", 0)
    if type(mark) is not int or mark < 0:
        raise FactsError([f"{path}: exported_through must be a row number; leave it as exported"])
    return mark


def _fact(entry: dict[str, Any], n: int) -> tuple[Fact | None, list[str]]:
    """One [[fact]] table, checked. Problems name the fact by id, or by position."""
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return None, [f"fact {n}: id is required"]
    if not _ID.fullmatch(raw_id):
        return None, [f"fact {n}: id {raw_id!r}: use letters, digits, '.', '_', ':' or '-'"]
    where = raw_id
    problems = [f"{where}: unknown field {k!r}" for k in sorted(set(entry) - FIELDS)]

    kind, status = entry.get("kind"), entry.get("status")
    if kind not in KINDS:
        problems.append(f"{where}: kind {kind!r} is not one of {', '.join(KINDS)}")
    if status not in ("draft", "verified"):
        problems.append(f"{where}: status {status!r} is not draft or verified")
    body = entry.get("text")
    if not isinstance(body, str) or not body.strip():
        problems.append(f"{where}: text is required")
    evidence = entry.get("evidence")
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        problems.append(f"{where}: evidence must be text")
        evidence = None

    verified_at = _date(entry.get("verified_at"), where, problems)

    level = entry.get("level")
    bad_level = False  # already reported, so not reported again as missing
    if level is not None and kind != "skill":
        problems.append(f"{where}: level is only for skills")
        level, bad_level = None, True
    elif level == "TODO":
        level = None  # not known yet: fine for a draft; a verified skill needs one
    elif level is not None and level not in LEVELS:
        problems.append(f"{where}: level {level!r} is not one of {', '.join(LEVELS)}")
        level, bad_level = None, True

    aliases = entry.get("aliases", [])
    if aliases and kind != "skill":
        problems.append(f"{where}: aliases are only for skills")
    elif not isinstance(aliases, list) or not all(
        isinstance(a, str) and a.strip() for a in aliases
    ):
        problems.append(f'{where}: aliases must be a list of names, e.g. ["Kubernetes", "k8s"]')

    # Verified facts are strict; drafts may be incomplete but are never citable.
    if status == "verified":
        if evidence is None:
            problems.append(f"{where}: a verified fact needs evidence")
        if verified_at is None and "verified_at" not in entry:
            problems.append(f"{where}: a verified fact needs verified_at")
        if kind == "skill" and level is None and not bad_level:
            problems.append(f"{where}: a verified skill needs a level ({', '.join(LEVELS)})")

    if problems:
        return None, problems
    return (
        Fact(
            fact_id=raw_id,
            kind=str(kind),
            text=str(body).strip(),
            status=str(status),
            evidence=evidence.strip() if evidence else None,
            verified_at=verified_at,
            level=level,
            aliases=tuple(a.strip() for a in aliases),
        ),
        [],
    )


def _date(value: Any, where: str, problems: list[str]) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            problems.append(f"{where}: verified_at {value!r} is not a date (YYYY-MM-DD)")
            return None
    if not isinstance(value, date) or hasattr(value, "hour"):
        problems.append(f"{where}: verified_at {value!r} is not a date (YYYY-MM-DD)")
        return None
    if value > date.today():
        problems.append(f"{where}: verified_at {value} is in the future")
        return None
    return value


def plan_import(engine: Engine, facts: list[Fact]) -> ImportPlan:
    """What import_facts would write, without writing anything."""
    with engine.connect() as conn:
        return _plan(conn, facts)


def import_facts(
    engine: Engine, facts: list[Fact], *, exported_through: int = 0, overwrite: bool = False
) -> ImportPlan:
    """Write new and changed facts as new versions, in one transaction. Safe
    to re-run: unchanged facts are skipped. Returns what it did.

    exported_through is the file's export mark. FactsError, and nothing
    written, if a fact was edited in the browser after it, unless overwrite."""
    with engine.begin() as conn:
        # One import at a time, so two can't number the same version.
        conn.execute(text("LOCK TABLE facts IN EXCLUSIVE MODE"))
        edited = _browser_edits_since(conn, exported_through)
        if edited and not overwrite:
            raise FactsError([f"{', '.join(edited)}: {EDITED_SINCE_EXPORT}"])
        plan = _plan(conn, facts)
        latest = _latest(conn)
        for fact in plan.new + plan.changed:
            before = latest.get(fact.fact_id)
            _insert(conn, fact, 1 if before is None else before.version + 1)
    return plan


EDITED_SINCE_EXPORT = (
    "edited in the browser since this file was exported. Export first"
    " (jhunt-facts export FILE) and make your changes there, or give --overwrite"
    " to let this file win"
)


def retire_fact(
    engine: Engine, fact_id: str, *, via: str = "command", version: int | None = None
) -> int:
    """Retire a fact: a new version with status retired. Returns its version.
    version: the one the owner saw; FactChanged if there's a newer one."""
    with engine.begin() as conn:
        conn.execute(text("LOCK TABLE facts IN EXCLUSIVE MODE"))
        latest = _current(conn, fact_id, version)
        retired = Fact(
            fact_id, latest.kind, latest.text, "retired", latest.evidence, latest.verified_at,
            latest.level, latest.aliases,
        )  # fmt: skip
        _insert(conn, retired, latest.version + 1, via=via)
    return latest.version + 1


def save_fact(engine: Engine, fact_id: str, changes: dict[str, Any], *, version: int) -> int | None:
    """An edit from the Facts page: the fact's latest version with changes
    (some of EDITABLE) applied, checked as an import checks a file, saved as
    a new version via the browser. Returns it, or None if nothing changed.

    version is the one the edit started from: FactChanged if there's a newer
    one, so an edit made on an old page never undoes a later one."""
    with engine.begin() as conn:
        conn.execute(text("LOCK TABLE facts IN EXCLUSIVE MODE"))
        latest = _current(conn, fact_id, version)
        entry: dict[str, Any] = {
            "id": fact_id, "kind": latest.kind, "text": latest.text,
            "evidence": latest.evidence, "verified_at": latest.verified_at,
            "status": latest.status, "level": latest.level, "aliases": list(latest.aliases),
        }  # fmt: skip
        for name, value in changes.items():
            if name not in EDITABLE:
                raise ValueError(f"{name} can't be edited on the Facts page")
            entry[name] = value
        entry = {k: v for k, v in entry.items() if v is not None}
        if not entry["aliases"]:
            del entry["aliases"]
        fact, problems = _fact(entry, 1)
        if fact is None:
            raise FactsError(problems)
        if fact.content_sha256() == _hash(conn, fact_id):
            return None
        _insert(conn, fact, latest.version + 1, via="browser")
    return latest.version + 1


def export_facts(engine: Engine) -> tuple[str, int]:
    """Every fact's latest version, retired ones left out, as a facts file
    that read_facts reads back unchanged. Returns (the file, how many facts).
    The file is marked with the newest row in the table."""
    with engine.connect() as conn:
        mark = conn.execute(text("SELECT coalesce(max(id), 0) FROM facts")).scalar_one()
        live = [f for f in sorted(_latest(conn).values(), key=lambda f: f.fact_id)
                if f.status != "retired"]  # fmt: skip
    lines = [
        "# Your facts, exported from jhunt (the database is the source of truth).",
        "# Edit and import as before. Leave exported_through as it is: it's how",
        "# import knows which Facts page edits this file already holds.",
        f"exported_through = {mark}",
    ]
    for f in live:
        lines += ["", "[[fact]]", f"id = {_toml(f.fact_id)}", f"kind = {_toml(f.kind)}",
                  f"text = {_toml(f.text)}"]  # fmt: skip
        if f.evidence is not None:
            lines.append(f"evidence = {_toml(f.evidence)}")
        if f.verified_at is not None:
            lines.append(f"verified_at = {f.verified_at.isoformat()}")  # a TOML date
        lines.append(f"status = {_toml(f.status)}")
        if f.level is not None:
            lines.append(f"level = {_toml(f.level)}")
        if f.aliases:
            lines.append(f"aliases = [{', '.join(_toml(a) for a in f.aliases)}]")
    return "\n".join(lines) + "\n", len(live)


def browser_edits_since(engine: Engine, mark: int) -> list[str]:
    """The facts edited in the browser after an export's mark."""
    with engine.connect() as conn:
        return _browser_edits_since(conn, mark)


def _browser_edits_since(conn: Connection, mark: int) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT fact_id FROM facts WHERE via = 'browser' AND id > :m ORDER BY fact_id"
        ),
        {"m": mark},
    )
    return [r.fact_id for r in rows]


def _current(conn: Connection, fact_id: str, version: int | None) -> FactVersion:
    """The fact's latest version, if it may still be changed."""
    latest = _latest(conn).get(fact_id)
    if latest is None:
        raise NoSuchFact([f"no fact {fact_id!r}"])
    if version is not None and latest.version != version:
        raise FactChanged([f"{fact_id} changed since you opened it: it's at v{latest.version}"])
    if latest.status == "retired":
        raise FactsError([f"{fact_id} is already retired (version {latest.version})"])
    return latest


def _hash(conn: Connection, fact_id: str) -> str:
    found: str = conn.execute(
        text("SELECT content_sha256 FROM facts WHERE fact_id = :f ORDER BY version DESC LIMIT 1"),
        {"f": fact_id},
    ).scalar_one()
    return found


def _toml(value: str) -> str:
    """A TOML basic string. JSON's escapes are TOML's, except that TOML also
    wants DEL escaped."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def latest_facts(engine: Engine) -> list[FactVersion]:
    """Each fact's latest version, by fact id."""
    with engine.connect() as conn:
        return sorted(_latest(conn).values(), key=lambda f: f.fact_id)


def citable_facts(engine: Engine) -> list[FactVersion]:
    """What a new draft may cite: each fact's latest version, if verified."""
    return [f for f in latest_facts(engine) if f.status == "verified"]


def _plan(conn: Connection, facts: list[Fact]) -> ImportPlan:
    latest = _latest(conn)
    # Each fact's latest content hash: equal means the file didn't change it.
    hashes: dict[str, str] = {
        r.fact_id: r.content_sha256
        for r in conn.execute(
            text(
                "SELECT DISTINCT ON (fact_id) fact_id, content_sha256 FROM facts"
                " ORDER BY fact_id, version DESC"
            )
        )
    }
    plan = ImportPlan()
    for fact in facts:
        before = latest.get(fact.fact_id)
        if before is None:
            plan.new.append(fact)
        elif before.status == "retired":
            plan.retired_in_file.append(fact.fact_id)  # only the Facts page un-retires
        elif hashes[fact.fact_id] == fact.content_sha256():
            plan.unchanged.append(fact.fact_id)
        else:
            plan.changed.append(fact)
    in_file = {f.fact_id for f in facts}
    # Retired facts are left out of an export, so not listed as missing.
    plan.missing = sorted(
        i for i, f in latest.items() if i not in in_file and f.status != "retired"
    )
    return plan


def _latest(conn: Connection) -> dict[str, FactVersion]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (fact_id) id, fact_id, version, kind, text, evidence,"
            " verified_at, status, level, aliases, via FROM facts ORDER BY fact_id, version DESC"
        )
    )
    return {r.fact_id: FactVersion(**{**r._mapping, "aliases": tuple(r.aliases)}) for r in rows}


def _insert(conn: Connection, fact: Fact, version: int, *, via: str = "command") -> None:
    conn.execute(
        text(
            "INSERT INTO facts (fact_id, version, kind, text, evidence, verified_at, status,"
            " level, aliases, content_sha256, created_by, via)"
            " VALUES (:fact_id, :version, :kind, :text, :evidence, :verified_at, :status,"
            " :level, :aliases, :sha, 'owner', :via)"
        ),
        {
            "fact_id": fact.fact_id,
            "version": version,
            "kind": fact.kind,
            "text": fact.text,
            "evidence": fact.evidence,
            "verified_at": fact.verified_at,
            "status": fact.status,
            "level": fact.level,
            "aliases": list(fact.aliases),
            "sha": fact.content_sha256(),
            "via": via,
        },
    )


def _content_sha256(
    kind: str, body: str, evidence: str | None, verified_at: date | None, status: str,
    level: str | None, aliases: tuple[str, ...],
) -> str:  # fmt: skip
    fields: list[object] = [
        kind, body, evidence, verified_at.isoformat() if verified_at else None, status, level,
    ]  # fmt: skip
    # Aliases count only when there are some, so a fact imported before they
    # existed keeps its hash and doesn't look changed (migration 0011).
    if aliases:
        fields.append(list(aliases))
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()
