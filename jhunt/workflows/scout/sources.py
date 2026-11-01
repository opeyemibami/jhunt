"""Scout's sources: <JHUNT_CONFIG_DIR>/sources.toml, in git (ADR 0010)."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SOURCES_FILE = "sources.toml"
PROVIDERS = frozenset({"greenhouse", "ashby"})
# A board slug becomes part of a URL and an object name.
_BOARD = re.compile(r"[A-Za-z0-9._-]+")


class SourcesError(Exception):
    """sources.toml is missing or wrong."""


@dataclass(frozen=True)
class Source:
    provider: str
    board: str
    company: str


def load_sources(config_dir: Path) -> list[Source]:
    path = config_dir / SOURCES_FILE
    if not path.is_file():
        raise SourcesError(f"sources file missing: {path} (resolved: {path.resolve()})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SourcesError(f"{path}: invalid TOML: {exc}") from None
    if set(data) != {"sources"} or not isinstance(data["sources"], list):
        raise SourcesError(f"{path}: must contain exactly one list, [[sources]]")

    sources: list[Source] = []
    for n, entry in enumerate(data["sources"], 1):
        where = f"{path}: source {n}"
        provider, board, company = (entry.get(k) for k in ("provider", "board", "company"))
        if provider not in PROVIDERS:
            raise SourcesError(f"{where}: unknown provider {provider!r} (known: greenhouse, ashby)")
        if not isinstance(board, str) or not _BOARD.fullmatch(board):
            raise SourcesError(f"{where}: board must be a slug like 'acme', not {board!r}")
        if not isinstance(company, str) or not company.strip():
            raise SourcesError(f"{where}: company must be a non-empty name")
        source = Source(provider, board, company.strip())
        if any((s.provider, s.board) == (provider, board) for s in sources):
            raise SourcesError(f"{where}: {provider} board {board!r} is listed twice")
        sources.append(source)
    return sources
