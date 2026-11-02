"""The claim validator: no claim without a fact (hard rule 6, ADR 0015).

A drafted claim is {text, fact_ids}, citing the owner's fact ids. check_claims
returns every claim in order with its text unchanged, accepted or blocked with
every reason that applies. It never rewords a claim. A claim is blocked if:

1. it cites no fact;
2. it cites a fact the bank doesn't have;
3. a cited fact's latest version isn't verified (draft or retired);
4. it has a number, in digits or spelled out, that its cited facts don't;
5. its words state a skill level above the level of a skill it cites or
   mentions: a skill can't borrow a level by citing another fact.

It checks citations, numbers and level words, not meaning (ADR 0015, Limits).
It is pure: the caller passes the facts (latest_facts) and the level words.
"""

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jhunt.platform.memory.facts import FactVersion

LEVELS = ("studied", "used", "built", "production")  # lowest first
LEVELS_FILE = "claim_levels.toml"

# A number in digits, with its unit if any: 35%, 1,200, 1.5M, 3x (or with the
# times sign, \u00d7), 10k, 2021.
# Not one inside a word (B2B, S3) or a longer number.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9.,])(\d+(?:[.,]\d+)*)\s?(%|x|\u00d7|k|bn|m|b)?(?![A-Za-z0-9])", re.I
)
# Numbers in words: blocked unless a cited fact uses the same word (owner).
_SPELLED = re.compile(
    r"(?<![A-Za-z])("
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen"
    r"|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty"
    r"|seventy|eighty|ninety|hundreds?|thousands?|millions?|billions?|dozens?"
    r")(?![A-Za-z])",
    re.I,
)


class ClaimLevelsError(Exception):
    """claim_levels.toml is missing or wrong."""


@dataclass(frozen=True)
class Claim:
    text: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class CheckedClaim:
    text: str  # exactly as drafted
    fact_ids: tuple[str, ...]
    versions: tuple[int, ...]  # the cited verified versions' row ids
    problems: tuple[str, ...]  # empty when accepted

    @property
    def blocked(self) -> bool:
        return bool(self.problems)


@dataclass(frozen=True)
class ClaimCheck:
    claims: list[CheckedClaim]  # every claim, in the order drafted

    @property
    def accepted(self) -> list[CheckedClaim]:
        return [c for c in self.claims if not c.blocked]

    @property
    def blocked(self) -> list[CheckedClaim]:
        return [c for c in self.claims if c.blocked]


@dataclass(frozen=True)
class ClaimLevels:
    """The words that state each skill level, by level, lowest first."""

    words: Mapping[str, tuple[str, ...]]


def load_claim_levels(config_dir: Path) -> ClaimLevels:
    path = config_dir / LEVELS_FILE
    if not path.is_file():
        raise ClaimLevelsError(f"claim levels file missing: {path} (resolved: {path.resolve()})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ClaimLevelsError(f"{path}: invalid TOML: {exc}") from None
    if set(data) != {"levels"} or not isinstance(data["levels"], dict):
        raise ClaimLevelsError(f"{path}: must contain exactly one table, [levels]")
    table = data["levels"]
    if set(table) != set(LEVELS):
        raise ClaimLevelsError(f"{path}: [levels] needs exactly the levels {', '.join(LEVELS)}")
    seen: dict[str, str] = {}
    for level in LEVELS:
        words = table[level]
        if not words or not all(isinstance(w, str) and w.strip() for w in words):
            raise ClaimLevelsError(f"{path}: {level}: a non-empty list of words")
        for word in words:
            if word.lower() in seen:
                raise ClaimLevelsError(
                    f"{path}: {word!r} is listed under both {seen[word.lower()]} and {level}"
                )
            seen[word.lower()] = level
    return ClaimLevels({level: tuple(table[level]) for level in LEVELS})


def check_claims(
    claims: Iterable[Claim], facts: Iterable[FactVersion], levels: ClaimLevels
) -> ClaimCheck:
    """Check each claim against each fact's latest version (latest_facts)."""
    by_id = {f.fact_id: f for f in facts}
    return ClaimCheck([_check(claim, by_id, levels) for claim in claims])


def _check(claim: Claim, facts: Mapping[str, FactVersion], levels: ClaimLevels) -> CheckedClaim:
    problems: list[str] = []
    cited: list[FactVersion] = []  # the claim's verified facts: all that can support it
    if not claim.fact_ids:
        problems.append("cites no fact")
    for fact_id in claim.fact_ids:
        found = facts.get(fact_id)
        if found is None:
            problems.append(f"cites unknown fact {fact_id!r}")
        elif found.status != "verified":
            problems.append(f"fact {fact_id!r} is {found.status}, not verified")
        else:
            cited.append(found)

    # Numbers: each must be in a cited fact, the same number with the same unit.
    supported = {n for f in cited for n in _numbers(f.text)}
    for number in _numbers(claim.text):
        if number not in supported:
            problems.append(f"number {number!r} is not in its cited facts")
    words = {w.lower() for f in cited for w in _SPELLED.findall(f.text)}
    for word in dict.fromkeys(w.lower() for w in _SPELLED.findall(claim.text)):
        if word not in words:
            problems.append(f"spelled-out number {word!r} is not in its cited facts")

    # Skill levels: the highest level the wording states, against every verified
    # skill the claim cites or mentions (owner, PR 3.6a review). "Deployed
    # Kubernetes in production" citing only a project is still about Kubernetes.
    stated = _stated_level(claim.text, levels)
    if stated is not None:
        level, word = stated
        skills = {f.fact_id: f for f in cited if f.kind == "skill"}
        for f in facts.values():
            if f.kind == "skill" and f.status == "verified" and _mentions(claim.text, f):
                skills.setdefault(f.fact_id, f)
        for f in skills.values():
            if f.level and LEVELS.index(level) > LEVELS.index(f.level):
                how = "" if f.fact_id in claim.fact_ids else " (mentioned, not cited)"
                problems.append(f"states {level}, above {f.fact_id!r} ({f.level}): {word!r}{how}")

    return CheckedClaim(
        text=claim.text,
        fact_ids=claim.fact_ids,
        versions=tuple(f.id for f in cited),
        problems=tuple(problems),
    )


def _numbers(text: str) -> set[str]:
    """Each number in digits as number+unit: 1,200 -> "1200"; 1.5M -> "1.5m";
    3x and 3 with the times sign -> "3x"."""
    found = set()
    for digits, unit in _NUMBER.findall(text):
        unit = (unit or "").lower().replace("\u00d7", "x")
        found.add(digits.replace(",", "") + unit)
    return found


def _stated_level(text: str, levels: ClaimLevels) -> tuple[str, str] | None:
    """The highest level whose words the text contains, and the word that said so."""
    for level in reversed(LEVELS):
        for word in levels.words[level]:
            if _phrase(word).search(text):
                return level, word
    return None


def _mentions(text: str, skill: FactVersion) -> bool:
    """Whether the text names the skill: by its aliases if it has any, else by
    its text split on "and", ",", "/" and parentheses ("Python (FastAPI,
    asyncio)" names Python, FastAPI and asyncio). Whole words, any case."""
    names = skill.aliases or tuple(
        n.strip() for n in re.split(r"\s+and\s+|[,/()]", skill.text, flags=re.I) if n.strip()
    )
    return any(_phrase(name).search(text) for name in names)


def _phrase(words: str) -> re.Pattern[str]:
    # Whole words, any case, any whitespace between them.
    body = r"\s+".join(re.escape(w) for w in words.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.I)
