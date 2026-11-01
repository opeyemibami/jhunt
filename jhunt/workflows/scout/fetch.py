"""The fetch_board tool: one GET of a board's job list, kept in staging (ADR 0010).

Its arguments are small and fixed for the step (provider, board, day) and its
result is a reference, never the list itself (ADR 0008). The list goes to the
staging bucket, create-only under scout/<day>/<provider>-<board>.json; a repeat
on the same day adopts what's there without fetching again. The gateway runs
it at level 1 under "fetch_postings".
"""

import json
import re
from typing import Any

import httpx

from jhunt.jobs.boards import MAX_API_BYTES, PUBLIC_APIS, board_list_url
from jhunt.platform.artifacts import StagingStore
from jhunt.platform.gateway import Tool
from jhunt.workflows.scout.sources import PROVIDERS

ACTION = "fetch_postings"
_BOARD = re.compile(r"[A-Za-z0-9._-]+")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class BoardFetchFailed(Exception):
    """The board's job list could not be fetched; nothing was stored."""


def fetch_board_tool(
    staging: StagingStore,
    http: httpx.Client,
    *,
    prefix: str = "scout/",
    apis: dict[str, str] = PUBLIC_APIS,
) -> Tool:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        provider, board, day = args["provider"], args["board"], args["day"]
        # All three become part of the object name.
        if provider not in PROVIDERS or not _BOARD.fullmatch(board) or not _DAY.fullmatch(day):
            raise ValueError(f"bad fetch_board arguments: {args!r}")
        key = f"{prefix}{day}/{provider}-{board}.json"

        found = staging.existing(key)
        if found is None:
            body = _get(http, board_list_url(provider, board, apis))
            found = staging.put_or_adopt(key, body, "application/json")
        record, data = found
        jobs = json.loads(data).get("jobs")
        if not isinstance(jobs, list):
            raise BoardFetchFailed(f"{provider} board {board!r}: no jobs list in the response")
        return {
            "uri": record.uri,
            "key": record.key,
            "generation": record.generation,
            "sha256": record.sha256,
            "jobs": len(jobs),
        }

    return Tool("fetch_board", ACTION, run)


def _get(http: httpx.Client, url: str) -> bytes:
    try:
        return _read(http, url)
    except httpx.HTTPError as exc:  # refused, timed out, reset: one board, not the run
        raise BoardFetchFailed(f"GET {url}: could not be reached ({exc})") from None


def _read(http: httpx.Client, url: str) -> bytes:
    with http.stream("GET", url, follow_redirects=True) as response:
        if response.status_code == 404:
            raise BoardFetchFailed(f"GET {url}: board not found")
        if response.status_code != 200:
            raise BoardFetchFailed(f"GET {url}: HTTP {response.status_code}")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_API_BYTES:
                raise BoardFetchFailed(f"GET {url}: larger than {MAX_API_BYTES} bytes")
    if not body.lstrip().startswith(b"{"):
        raise BoardFetchFailed(f"GET {url}: no jobs list in the response (not JSON)")
    return bytes(body)
