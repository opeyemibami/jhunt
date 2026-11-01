"""Scout: read the job boards in config/sources.toml, keep their postings with
a scope for a search in Ireland, and score them (ADR 0010). Public API for the
composition root.
"""

from jhunt.workflows.scout.fetch import BoardFetchFailed, fetch_board_tool
from jhunt.workflows.scout.location import Scope, ScopeError, load_scope
from jhunt.workflows.scout.parse import Posting, parse_board
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.scout.sources import Source, load_sources

__all__ = [
    "BoardFetchFailed",
    "Posting",
    "Scope",
    "ScopeError",
    "Source",
    "fetch_board_tool",
    "load_scope",
    "load_sources",
    "parse_board",
    "upsert_postings",
]
