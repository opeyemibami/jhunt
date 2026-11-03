"""Scout: read the job boards in config/sources.toml, keep their postings with
a scope for a search in Ireland, and score them (ADRs 0010 and 0011). Public
API for the composition root.
"""

from jhunt.workflows.scout.fetch import BoardFetchFailed, fetch_board_tool
from jhunt.workflows.scout.fit import Targets, TargetsError, fit_score, load_targets
from jhunt.workflows.scout.graph import (
    VERSION,
    VERSIONS,
    WORKFLOW,
    build,
    scout_input,
    summary,
    thread_id_for,
)
from jhunt.workflows.scout.hidden import hidden_keys, hidden_postings, hide_posting, unhide
from jhunt.workflows.scout.location import Scope, ScopeError, load_scope
from jhunt.workflows.scout.parse import Posting, parse_board
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.scout.sources import Source, SourcesError, load_sources
from jhunt.workflows.scout.start import (
    Mode,
    ScoutRefused,
    Today,
    latest_today,
    run_summary,
    start_scout,
    utc_today,
)
from jhunt.workflows.scout.store import store_postings_tool

__all__ = [
    "VERSION",
    "VERSIONS",
    "WORKFLOW",
    "BoardFetchFailed",
    "Mode",
    "Posting",
    "Scope",
    "ScopeError",
    "ScoutRefused",
    "Source",
    "SourcesError",
    "Targets",
    "TargetsError",
    "Today",
    "build",
    "fetch_board_tool",
    "fit_score",
    "hidden_keys",
    "hidden_postings",
    "hide_posting",
    "latest_today",
    "load_scope",
    "load_sources",
    "load_targets",
    "parse_board",
    "run_summary",
    "scout_input",
    "start_scout",
    "store_postings_tool",
    "summary",
    "thread_id_for",
    "unhide",
    "upsert_postings",
    "utc_today",
]
