"""The fact bank: the owner's facts, one row per version (ADR 0014).

The owner keeps their facts in their own TOML file, one [[fact]] table each,
outside git. read_facts validates the whole file: one bad fact rejects all of
it, with every problem listed. import_facts writes what's new or changed as
new versions, skips what's unchanged, and only lists facts that are missing
from the file or retired. A row is never changed (migration 0010), so a draft
keeps citing the version it used.

Only a fact's latest version, and only when it is verified, is citable.
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
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


class FactsError(Exception):
    """The facts file, or a request about a fact, can't be used. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


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
    if set(data) != {"fact"} or not isinstance(data["fact"], list):
        raise FactsError([f"{path}: must hold only [[fact]] tables"])

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


def import_facts(engine: Engine, facts: list[Fact]) -> ImportPlan:
    """Write new and changed facts as new versions, in one transaction. Safe
    to re-run: unchanged facts are skipped. Returns what it did."""
    with engine.begin() as conn:
        # One import at a time, so two can't number the same version.
        conn.execute(text("LOCK TABLE facts IN EXCLUSIVE MODE"))
        plan = _plan(conn, facts)
        latest = _latest(conn)
        for fact in plan.new + plan.changed:
            before = latest.get(fact.fact_id)
            _insert(conn, fact, 1 if before is None else before.version + 1)
    return plan


def retire_fact(engine: Engine, fact_id: str) -> int:
    """Retire a fact: a new version with status retired. Returns its version."""
    with engine.begin() as conn:
        conn.execute(text("LOCK TABLE facts IN EXCLUSIVE MODE"))
        latest = _latest(conn).get(fact_id)
        if latest is None:
            raise FactsError([f"no fact {fact_id!r}"])
        if latest.status == "retired":
            raise FactsError([f"{fact_id} is already retired (version {latest.version})"])
        retired = Fact(
            fact_id, latest.kind, latest.text, "retired", latest.evidence, latest.verified_at,
            latest.level, latest.aliases,
        )  # fmt: skip
        _insert(conn, retired, latest.version + 1)
    return latest.version + 1


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
    plan.missing = sorted(i for i in latest if i not in in_file)
    return plan


def _latest(conn: Connection) -> dict[str, FactVersion]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (fact_id) id, fact_id, version, kind, text, evidence,"
            " verified_at, status, level, aliases FROM facts ORDER BY fact_id, version DESC"
        )
    )
    return {r.fact_id: FactVersion(**{**r._mapping, "aliases": tuple(r.aliases)}) for r in rows}


def _insert(conn: Connection, fact: Fact, version: int) -> None:
    conn.execute(
        text(
            "INSERT INTO facts (fact_id, version, kind, text, evidence, verified_at, status,"
            " level, aliases, content_sha256, created_by)"
            " VALUES (:fact_id, :version, :kind, :text, :evidence, :verified_at, :status,"
            " :level, :aliases, :sha, 'owner')"
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
