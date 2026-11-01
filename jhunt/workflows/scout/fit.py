"""The fit score: how well a posting matches the owner's targets (ADR 0011).

Deterministic, no model, from <JHUNT_CONFIG_DIR>/targets.toml (in git):

    0 if an exclude keyword is in the title; otherwise
    title weight if a title phrase is in the title
    + include weight per keyword in the title or description, up to its max
    - penalty weight if a penalty keyword is in the title
    clamped to 0.0..1.0.

Matching ignores case and needs whole words, so "ml" doesn't match "html" and
"intern" doesn't match "internal". The matches are returned with the score, so
the digest can show why a posting scored what it did.
"""

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jhunt.jobs.text import html_to_text

TARGETS_FILE = "targets.toml"
_SECTIONS = {
    "title": ("weight", "phrases"),
    "include": ("weight", "max", "keywords"),
    "exclude": ("keywords",),
    "penalty": ("weight", "keywords"),
}


class TargetsError(Exception):
    """targets.toml is missing or wrong."""


@dataclass(frozen=True)
class Targets:
    title_weight: float
    title_phrases: list[str]
    include_weight: float
    include_max: float
    include_keywords: list[str]
    exclude_keywords: list[str]
    penalty_weight: float
    penalty_keywords: list[str]

    def as_input(self) -> dict[str, Any]:
        """For a thread's input and the store_postings arguments (plain JSON)."""
        return asdict(self)


def load_targets(config_dir: Path) -> Targets:
    path = config_dir / TARGETS_FILE
    if not path.is_file():
        raise TargetsError(f"targets file missing: {path} (resolved: {path.resolve()})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise TargetsError(f"{path}: invalid TOML: {exc}") from None
    if set(data) != set(_SECTIONS):
        raise TargetsError(f"{path}: needs exactly the sections {sorted(_SECTIONS)}")
    values: dict[str, Any] = {}
    for section, fields in _SECTIONS.items():
        if set(data[section]) != set(fields):
            raise TargetsError(f"{path}: [{section}] needs exactly {list(fields)}")
        for name in fields:
            value, where = data[section][name], f"{path}: [{section}] {name}"
            if name in ("weight", "max"):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not 0 <= value <= 1
                ):
                    raise TargetsError(f"{where} must be a number between 0 and 1")
                value = float(value)
            elif not value or not all(isinstance(v, str) and v.strip() for v in value):
                raise TargetsError(f"{where} must be a non-empty list of non-empty strings")
            values[f"{section}_{name}"] = value
    return Targets(**values)


def fit_score(
    title: str, description_html: str, targets: Targets
) -> tuple[float, dict[str, list[str]]]:
    """(score, matches). matches lists what matched, by section; empty ones are left out."""
    description = html_to_text(description_html.encode())
    excluded = _found(targets.exclude_keywords, title)
    if excluded:
        return 0.0, {"exclude": excluded}
    matches = {
        "title": _found(targets.title_phrases, title),
        "include": _found(targets.include_keywords, f"{title}\n{description}"),
        "penalty": _found(targets.penalty_keywords, title),
    }
    score = targets.title_weight if matches["title"] else 0.0
    score += min(targets.include_weight * len(matches["include"]), targets.include_max)
    score -= targets.penalty_weight if matches["penalty"] else 0.0
    # round(): 0.6 + 0.08 * 3 is 0.8399999999999999 in floating point.
    return round(min(max(score, 0.0), 1.0), 4), {k: v for k, v in matches.items() if v}


def _found(phrases: list[str], text: str) -> list[str]:
    return [p for p in phrases if _pattern(p).search(text)]


def _pattern(phrase: str) -> re.Pattern[str]:
    # Whole words: no letter or digit on either side. Any run of whitespace
    # between the phrase's words matches.
    words = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"(?<![a-z0-9]){words}(?![a-z0-9])", re.I)
