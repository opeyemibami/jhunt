"""Scout: read the job boards in config/sources.toml, keep their postings with
a scope for a search in Ireland, and score them (ADR 0010). Public API for the
composition root.
"""

from jhunt.workflows.scout.fetch import BoardFetchFailed, fetch_board_tool
from jhunt.workflows.scout.graph import (
    VERSION,
    WORKFLOW,
    build,
    scout_input,
    summary,
    thread_id_for,
)
from jhunt.workflows.scout.location import Scope, ScopeError, load_scope
from jhunt.workflows.scout.parse import Posting, parse_board
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.scout.sources import Source, SourcesError, load_sources
from jhunt.workflows.scout.store import store_postings_tool

__all__ = [
    "VERSION",
    "WORKFLOW",
    "BoardFetchFailed",
    "Posting",
    "Scope",
    "ScopeError",
    "Source",
    "SourcesError",
    "build",
    "fetch_board_tool",
    "load_scope",
    "load_sources",
    "parse_board",
    "scout_input",
    "store_postings_tool",
    "summary",
    "thread_id_for",
    "upsert_postings",
]
