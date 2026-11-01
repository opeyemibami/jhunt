"""fetch_board: one GET of a board's job list, kept in staging; returns a reference."""

import hashlib
import json

import httpx
import pytest

from jhunt.platform.artifacts import StagingStore
from jhunt.workflows.scout.fetch import BoardFetchFailed, fetch_board_tool
from tests.runtime.local_site import Site

BOARD = {"jobs": [{"id": 1}, {"id": 2}]}


def tool_for(site: Site, staging: StagingStore, prefix: str):  # type: ignore[no-untyped-def]
    apis = {"greenhouse": site.base, "ashby": site.base, "lever": site.base}
    return fetch_board_tool(staging, httpx.Client(timeout=10), prefix=prefix, apis=apis)


def test_stores_the_list_and_returns_a_reference(
    site: Site, staging: StagingStore, record_prefix: str
) -> None:
    body = json.dumps(BOARD).encode()
    site.pages["v1/boards/acme/jobs?content=true"] = (200, body)
    result = tool_for(site, staging, record_prefix).run(
        {"provider": "greenhouse", "board": "acme", "day": "2026-11-01"}
    )
    assert result["uri"].endswith(f"{record_prefix}2026-11-01/greenhouse-acme.json")
    assert (result["jobs"], result["sha256"]) == (2, hashlib.sha256(body).hexdigest())
    key = f"{record_prefix}2026-11-01/greenhouse-acme.json"
    assert staging.get(key, result["generation"], result["sha256"]) == body


def test_a_repeat_adopts_the_days_list_without_fetching(
    site: Site, staging: StagingStore, record_prefix: str
) -> None:
    site.pages["posting-api/job-board/adaption"] = (200, json.dumps(BOARD).encode())
    tool = tool_for(site, staging, record_prefix)
    args = {"provider": "ashby", "board": "adaption", "day": "2026-11-01"}
    first = tool.run(args)
    site.hits.clear()
    assert tool.run(args) == first
    assert site.hits == []


def test_a_missing_board_fails(site: Site, staging: StagingStore, record_prefix: str) -> None:
    with pytest.raises(BoardFetchFailed, match="board not found"):
        tool_for(site, staging, record_prefix).run(
            {"provider": "ashby", "board": "nobody", "day": "2026-11-01"}
        )


def test_a_response_without_jobs_fails(
    site: Site, staging: StagingStore, record_prefix: str
) -> None:
    site.pages["posting-api/job-board/odd"] = (200, b'{"error": "nope"}')
    with pytest.raises(BoardFetchFailed, match="no jobs list"):
        tool_for(site, staging, record_prefix).run(
            {"provider": "ashby", "board": "odd", "day": "2026-11-01"}
        )


def test_a_board_that_cannot_be_reached_fails(staging: StagingStore, record_prefix: str) -> None:
    # Port 9 (discard) is closed locally: the connection is refused.
    apis = {"greenhouse": "http://127.0.0.1:9", "ashby": "http://127.0.0.1:9"}
    tool = fetch_board_tool(staging, httpx.Client(timeout=5), prefix=record_prefix, apis=apis)
    with pytest.raises(BoardFetchFailed, match="could not be reached"):
        tool.run({"provider": "ashby", "board": "acme", "day": "2026-11-01"})


@pytest.mark.parametrize(
    "args",
    [
        {"provider": "lever", "board": "x", "day": "2026-11-01"},
        {"provider": "ashby", "board": "../x", "day": "2026-11-01"},
        {"provider": "ashby", "board": "x", "day": "today"},
    ],
)
def test_bad_arguments_are_refused(
    staging: StagingStore, record_prefix: str, args: dict[str, str]
) -> None:
    # They become part of an object name.
    tool = fetch_board_tool(staging, httpx.Client(), prefix=record_prefix)
    with pytest.raises(ValueError):
        tool.run(args)
