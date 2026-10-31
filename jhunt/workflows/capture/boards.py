"""Job links on the big job boards, read from each board's public API (ADR 0007).

A closed or missing job's page on these boards redirects to the board's job
list, which returns 200 with plenty of text. Fetched as a page, it would be
captured as the job. The board's API answers the real question instead: does
this job exist, and what is its description? A missing job is a 404, or is
absent from the board's list.

Companies also embed these boards in their own careers page, with the job in
the query (?gh_jid=<id>, ?ashby_jid=<uuid>). The page itself is only a shell
or the company's job list; the board's name is somewhere in its HTML. So the
page is fetched once to find the board, and the job comes from the API.
"""

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

# Where each board's public API lives. Tests point these at a local server.
PUBLIC_APIS = {
    "greenhouse": "https://boards-api.greenhouse.io",
    "ashby": "https://api.ashbyhq.com",
    "lever": "https://api.lever.co",
}
# An Ashby board's list holds every job's description, so it can be large.
MAX_API_BYTES = 20_000_000

_GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}
_UUID = re.compile(r"[0-9a-fA-F-]{36}")

# Where an embedding page names its board. Greenhouse: the embed script
# (boards.greenhouse.io/embed/job_board/js?for=<board>) or any link to the
# board. Ashby: the embed script or any link (jobs.ashbyhq.com/<org>/...).
# EU-hosted Greenhouse boards are left out: their API host isn't documented.
_EMBEDDED_BOARD = {
    "greenhouse": [
        re.compile(r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
        re.compile(r"(?<![.\w])(?:job-)?boards\.greenhouse\.io/(?!embed\b)([A-Za-z0-9_-]+)"),
    ],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)")],
}


class JobNotFound(Exception):
    """The board says the job doesn't exist (any more)."""


@dataclass(frozen=True)
class BoardJob:
    kind: str  # "greenhouse", "ashby" or "lever"
    board: str  # the board's slug in the URL
    job_id: str


def board_job(url: str) -> BoardJob | None:
    """The board and job id in a job link, or None if it isn't one."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = [segment for segment in parts.path.split("/") if segment]
    if host in _GREENHOUSE_HOSTS and len(path) >= 3 and path[1] == "jobs" and path[2].isdigit():
        return BoardJob("greenhouse", path[0], path[2])
    if host == "jobs.ashbyhq.com" and len(path) >= 2 and _UUID.fullmatch(path[1]):
        return BoardJob("ashby", path[0], path[1])
    if host == "jobs.lever.co" and len(path) >= 2 and _UUID.fullmatch(path[1]):
        return BoardJob("lever", path[0], path[1])
    return None


def embedded_job_id(url: str) -> tuple[str, str] | None:
    """(board kind, job id) for a careers-page link that names an embedded job."""
    query = parse_qs(urlsplit(url.strip()).query)
    greenhouse = query.get("gh_jid", [""])[0]
    if greenhouse.isdigit():
        return "greenhouse", greenhouse
    ashby = query.get("ashby_jid", [""])[0]
    if _UUID.fullmatch(ashby):
        return "ashby", ashby
    return None


def embedded_board(kind: str, page: bytes) -> str | None:
    """The board name an embedding page uses, or None if it names none, or
    several different ones (then there's no telling which holds the job)."""
    text = page.decode("utf-8", errors="replace")
    found = {match for pattern in _EMBEDDED_BOARD[kind] for match in pattern.findall(text)}
    return found.pop() if len(found) == 1 else None


def job_description(http: httpx.Client, job: BoardJob, apis: dict[str, str]) -> bytes:
    """The job's description HTML, from the board's API. Raises JobNotFound."""
    base = apis[job.kind]
    if job.kind == "greenhouse":
        data = _get_json(http, f"{base}/v1/boards/{job.board}/jobs/{job.job_id}?content=true")
        # Greenhouse returns the HTML entity-escaped ("&lt;p&gt;...").
        description = html.unescape(data.get("content") or "")
    elif job.kind == "ashby":
        board = _get_json(http, f"{base}/posting-api/job-board/{job.board}")
        found = [j for j in board.get("jobs", []) if j.get("id") == job.job_id]
        if not found:
            raise JobNotFound(job.job_id)
        description = found[0].get("descriptionHtml") or ""
    else:  # lever
        data = _get_json(http, f"{base}/v0/postings/{job.board}/{job.job_id}")
        # Lever splits a posting into a description, titled lists and a closing part.
        lists = "".join(
            f"<h3>{item.get('text', '')}</h3><ul>{item.get('content', '')}</ul>"
            for item in data.get("lists", [])
        )
        description = (data.get("description") or "") + lists + (data.get("additional") or "")
    if not description.strip():
        raise JobNotFound(f"{job.job_id}: the board returned no description")
    return description.encode()


def _get_json(http: httpx.Client, url: str) -> dict[str, Any]:
    with http.stream("GET", url, follow_redirects=True) as response:
        if response.status_code == 404:
            raise JobNotFound(url)
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_API_BYTES:
                raise ValueError(f"GET {url}: larger than {MAX_API_BYTES} bytes")
    data: dict[str, Any] = json.loads(body)
    return data
