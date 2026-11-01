"""The store_postings tool: one board's list from staging into postings (ADR 0011).

It reads exactly the generation fetch_board stored, checks its sha256, parses
it with the thread's scope settings, scores each posting against the thread's
targets (fit.py) and upserts the postings. Its arguments are that reference
plus the source, scope and targets, all fixed for the thread, and its result
is three counts (ADR 0008). A Scout v1 thread passes no targets: its postings
are stored unscored, as before. The gateway runs it at level 1 under
"fetch_postings" (fetch, dedupe, score postings).

The upsert is safe to repeat: a re-run refreshes the same rows.
"""

import dataclasses
from typing import Any

from sqlalchemy import Engine

from jhunt.platform.artifacts import StagingStore
from jhunt.platform.gateway import Tool
from jhunt.workflows.scout.fetch import ACTION
from jhunt.workflows.scout.fit import Targets, fit_score
from jhunt.workflows.scout.location import Scope
from jhunt.workflows.scout.parse import Posting, parse_board
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.scout.sources import Source


def store_postings_tool(engine: Engine, staging: StagingStore) -> Tool:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        payload = staging.get(args["key"], args["generation"], args["sha256"])
        source = Source(args["provider"], args["board"], args["company"])
        scope = Scope(northern_ireland_in_scope=args["northern_ireland_in_scope"])
        postings = parse_board(source, payload, scope)
        if "targets" in args:
            targets = Targets(**args["targets"])
            postings = [_scored(p, targets) for p in postings]
        counts = upsert_postings(engine, postings)
        return counts | {"in_scope": sum(p.in_scope for p in postings)}

    return Tool("store_postings", ACTION, run)


def _scored(posting: Posting, targets: Targets) -> Posting:
    score, matches = fit_score(posting.title, posting.description_html, targets)
    return dataclasses.replace(posting, fit_score=score, fit_matches=matches)
