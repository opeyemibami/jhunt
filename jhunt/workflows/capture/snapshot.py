"""The snapshot_posting tool: fetch a posting and keep it as records (ADR 0007).

Fetching and storing are one tool, so its arguments stay small and fixed for
the step (the URL and the dedupe key) and its result is only references
(ADR 0008). The gateway runs it at level 1 under "snapshot_job_description".

It is safe to repeat, which the gateway needs (a crash between running a tool
and recording its result runs it again): both uploads are create-only under
names derived from the dedupe key, and an object already there is adopted.
"""

import re
from typing import Any

import httpx

from jhunt.platform.artifacts import RecordStore, StoredRecord
from jhunt.platform.gateway import Tool
from jhunt.workflows.capture.boards import PUBLIC_APIS, JobNotFound, board_job, job_description
from jhunt.workflows.capture.text import html_to_text

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
            # might be different, or a JavaScript shell (ADR 0007).
            html_record, kept_html = kept
        else:
            html = _job_html(http, url, max_bytes, apis)
            html_record, kept_html = store.put_or_adopt(f"{folder}page.html", html, "text/html")
        # The text comes from the HTML that was kept, which may be an earlier
        # fetch than this one, so the two records always describe one page.
        text = html_to_text(kept_html).encode()
        text_record, _ = store.put_or_adopt(f"{folder}page.txt", text, "text/plain; charset=utf-8")
        return {"html": _reference(html_record), "text": _reference(text_record)}

    return Tool("snapshot_posting", ACTION, run)


def _job_html(http: httpx.Client, url: str, max_bytes: int, apis: dict[str, str]) -> bytes:
    """The HTML to keep for a job link. Raises FetchFailed with the reason."""
    job = board_job(url)
    if job is not None:
        # Greenhouse, Ashby and Lever: ask the board's API, never the page
        # (a closed job's page redirects to the board's list; boards.py).
        try:
            return job_description(http, job, apis)
        except JobNotFound:
            raise FetchFailed("job not found") from None
    html = _fetch(http, url, max_bytes)
    chars = len(html_to_text(html))
    if chars < MIN_TEXT_CHARS:
        raise FetchFailed(f"page looks JavaScript-rendered: {chars} chars of text")
    return html


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
