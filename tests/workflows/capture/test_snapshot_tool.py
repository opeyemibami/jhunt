"""snapshot_posting: fetch a posting and keep its HTML and text as records.

Real CI bucket, under the test's own prefix; a local web server for the pages.
"""

import hashlib

import httpx
import pytest
from google.cloud import storage

from jhunt.platform.artifacts import RecordStore
from jhunt.workflows.capture.snapshot import FetchFailed, snapshot_posting_tool
from tests.workflows.capture.conftest import Site

KEY = "a" * 64  # a dedupe key: 64 hex characters
# A real job description has well over 500 characters of text (the guard
# against JavaScript-rendered pages, ADR 0007).
ABOUT = (
    "We are hiring a senior engineer to build reliable data platforms. You will design "
    "durable workflows, own Postgres schemas, and work with product on what to build next. "
) * 4
PAGE = f"<html><body><h1>Senior Engineer</h1><p>Dublin</p><p>{ABOUT}</p></body></html>".encode()
EDITED = f"<html><body><h1>Edited later</h1><p>{ABOUT}</p></body></html>".encode()


def test_stores_html_and_text_and_returns_only_references(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    site.pages["job/1"] = (200, PAGE)
    tool = snapshot_posting_tool(store, http, prefix=record_prefix)

    result = tool.run({"url": site.url("job/1"), "dedupe_key": KEY})

    assert set(result) == {"html", "text"}
    assert set(result["html"]) == {"uri", "generation", "sha256"}  # references, no content
    assert result["html"]["uri"].endswith(f"{record_prefix}{KEY}/page.html")
    assert result["html"]["sha256"] == hashlib.sha256(PAGE).hexdigest()
    html = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    text = store.get(
        f"{record_prefix}{KEY}/page.txt", result["text"]["generation"], result["text"]["sha256"]
    )
    assert html == PAGE
    assert text == f"Senior Engineer\nDublin\n{ABOUT.strip()}".encode()


def test_a_second_fetch_with_different_html_keeps_the_first_snapshot(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    tool = snapshot_posting_tool(store, http, prefix=record_prefix)
    site.pages["job/1"] = (200, PAGE)
    first = tool.run({"url": site.url("job/1"), "dedupe_key": KEY})

    # The page changed by the time the step ran again (a retry after a crash).
    site.pages["job/1"] = (200, EDITED)
    second = tool.run({"url": site.url("job/1"), "dedupe_key": KEY})

    assert second == first
    assert second["html"]["sha256"] == hashlib.sha256(PAGE).hexdigest()
    assert site.hits == ["job/1", "job/1"]  # it did fetch again, and kept the first


def test_text_comes_from_the_stored_html_not_a_newer_fetch(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    # An earlier attempt stored the HTML, then died before storing the text.
    store.put(f"{record_prefix}{KEY}/page.html", PAGE, "text/html")
    site.pages["job/1"] = (200, EDITED)

    result = snapshot_posting_tool(store, http, prefix=record_prefix).run(
        {"url": site.url("job/1"), "dedupe_key": KEY}
    )

    text = store.get(
        f"{record_prefix}{KEY}/page.txt", result["text"]["generation"], result["text"]["sha256"]
    )
    assert text == f"Senior Engineer\nDublin\n{ABOUT.strip()}".encode()  # the kept HTML


@pytest.mark.parametrize("status", [404, 500])
def test_a_failed_fetch_stores_nothing(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_test_bucket: str,
    record_prefix: str,
    status: int,
) -> None:
    site.pages["job/1"] = (status, b"nope")
    tool = snapshot_posting_tool(store, http, prefix=record_prefix)
    with pytest.raises(FetchFailed, match=str(status)):
        tool.run({"url": site.url("job/1"), "dedupe_key": KEY})
    stored = list(storage.Client().list_blobs(gcs_test_bucket, prefix=record_prefix))
    assert stored == []


def test_a_javascript_rendered_page_is_refused_before_storing(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_test_bucket: str,
    record_prefix: str,
) -> None:
    # What an Ashby or other single-page app serves: a shell, filled in by JS.
    site.pages["job/js"] = (
        200,
        b'<html><head><script src="/app.js"></script></head>'
        b'<body><div id="root">Loading...</div></body></html>',
    )
    tool = snapshot_posting_tool(store, http, prefix=record_prefix)
    with pytest.raises(FetchFailed, match=r"page looks JavaScript-rendered: 10 chars of text"):
        tool.run({"url": site.url("job/js"), "dedupe_key": KEY})
    assert list(storage.Client().list_blobs(gcs_test_bucket, prefix=record_prefix)) == []


def test_an_oversized_page_is_refused(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    site.pages["job/big"] = (200, b"x" * 1024)
    tool = snapshot_posting_tool(store, http, prefix=record_prefix, max_bytes=512)
    with pytest.raises(FetchFailed, match="larger than 512 bytes"):
        tool.run({"url": site.url("job/big"), "dedupe_key": KEY})


@pytest.mark.parametrize("bad", ["", "../escape", "A" * 64, "a" * 63])
def test_the_key_must_be_a_dedupe_key(
    store: RecordStore, http: httpx.Client, record_prefix: str, bad: str
) -> None:
    # The key becomes part of an object name, so only a real sha256 is accepted.
    tool = snapshot_posting_tool(store, http, prefix=record_prefix)
    with pytest.raises(ValueError, match="dedupe_key"):
        tool.run({"url": "http://127.0.0.1/x", "dedupe_key": bad})
