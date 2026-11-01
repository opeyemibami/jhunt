"""The snapshot_posting tool: fetch a posting and keep it as records (ADR 0007).

Fetching and storing are one tool, so its arguments stay small and fixed for
the step (the URL and the dedupe key) and its result is only references
(ADR 0008). The gateway runs it at level 1 under "snapshot_job_description".

It is safe to repeat, which the gateway needs (a crash between running a tool
and recording its result runs it again): both uploads are create-only under
names derived from the dedupe key, and an object already there is adopted.
"""

import json
import re
from typing import Any

import httpx

from jhunt.jobs.boards import (
    PUBLIC_APIS,
    BoardJob,
    JobDetails,
    JobNotFound,
    board_job,
    embedded_board,
    embedded_job_id,
    job_description,
)
from jhunt.jobs.text import html_to_text
from jhunt.platform.artifacts import RecordStore, StoredRecord
from jhunt.platform.gateway import Tool

ACTION = "snapshot_job_description"
# A job page bigger than this is not a job page.
MAX_BYTES = 5_000_000
# A job description has far more text than this. Less means the page builds
# its content with JavaScript (Ashby, among others), and the HTML we fetched is
# an empty shell: refuse it rather than keep a snapshot with nothing in it.
MIN_TEXT_CHARS = 500
_DEDUPE_KEY = re.compile(r"[0-9a-f]{64}")


class FetchFailed(Exception):
    """The posting's page could not be fetched; nothing was stored."""


def snapshot_posting_tool(
    store: RecordStore,
    http: httpx.Client,
    *,
    prefix: str = "snapshots/",
    max_bytes: int = MAX_BYTES,
    apis: dict[str, str] = PUBLIC_APIS,
) -> Tool:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        url, key = args["url"], args["dedupe_key"]
        # The key becomes part of the object names: accept only a real sha256.
        if not _DEDUPE_KEY.fullmatch(key):
            raise ValueError(f"not a dedupe_key: {key!r}")
        folder = f"{prefix}{key}/"
        kept = store.existing(f"{folder}page.html")
        if kept is not None:
            # An earlier attempt kept this page (and crashed before the gateway
            # recorded the result): adopt it without fetching. A newer fetch
            # might be different, or a JavaScript shell (ADR 0007). Its job
            # details, if it was a board job, were kept before the page.
            html_record, kept_html = kept
            found = store.existing(f"{folder}job.json")
            job = json.loads(found[1]) if found is not None else None
        else:
            html, details = _job_html(http, url, max_bytes, apis)
            job = None
            if details is not None:
                # The board's own title and company, kept before the page so a
                # retry that adopts the page still has them.
                _, kept_job = store.put_or_adopt(
                    f"{folder}job.json",
                    json.dumps(details, sort_keys=True).encode(),
                    "application/json",
                )
                job = json.loads(kept_job)
            html_record, kept_html = store.put_or_adopt(f"{folder}page.html", html, "text/html")
        # The text comes from the HTML that was kept, which may be an earlier
        # fetch than this one, so the two records always describe one page.
        text = html_to_text(kept_html).encode()
        text_record, _ = store.put_or_adopt(f"{folder}page.txt", text, "text/plain; charset=utf-8")
        return {"html": _reference(html_record), "text": _reference(text_record), "job": job}

    return Tool("snapshot_posting", ACTION, run)


def _job_html(
    http: httpx.Client, url: str, max_bytes: int, apis: dict[str, str]
) -> tuple[bytes, dict[str, Any] | None]:
    """The HTML to keep for a job link, and the board's details for a board job
    (provider, board, job id, title, company). Raises FetchFailed with the reason."""
    job = board_job(url)
    embedded = embedded_job_id(url)
    if job is None and embedded is not None:
        # A board embedded in a company's careers page: fetch the page once,
        # only to find the board's name in it. Never keep the page itself.
        kind, job_id = embedded
        board = embedded_board(kind, _fetch(http, url, max_bytes))
        if board is None:
            raise FetchFailed("embedded board not found")
        job = BoardJob(kind, board, job_id)
    if job is not None:
        # Greenhouse, Ashby and Lever: ask the board's API, never the page
        # (a closed job's page redirects to the board's list; boards.py).
        try:
            details: JobDetails = job_description(http, job, apis)
        except JobNotFound:
            raise FetchFailed("job not found") from None
        return details.html, {
            "provider": job.kind,
            "board": job.board,
            "job_id": job.job_id,
            "title": details.title,
            "company": details.company,
        }
    html = _fetch(http, url, max_bytes)
    chars = len(html_to_text(html))
    if chars < MIN_TEXT_CHARS:
        raise FetchFailed(f"page looks JavaScript-rendered: {chars} chars of text")
    return html, None


def _fetch(http: httpx.Client, url: str, max_bytes: int) -> bytes:
    with http.stream("GET", url, follow_redirects=True) as response:
        if response.status_code != 200:
            raise FetchFailed(f"GET {url}: HTTP {response.status_code}")
        # A job link that lands somewhere else (the careers page, a search)
        # usually means the job closed. Same path, other scheme or host (http
        # to https, www.) or a trailing slash is fine.
        asked, landed = httpx.URL(url), response.url
        if asked.path.rstrip("/") != landed.path.rstrip("/"):
            raise FetchFailed(f"redirected from {url} to {landed}; the job may have closed")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise FetchFailed(f"GET {url}: larger than {max_bytes} bytes")
    return bytes(body)


def _reference(record: StoredRecord) -> dict[str, Any]:
    return {"uri": record.uri, "generation": record.generation, "sha256": record.sha256}
