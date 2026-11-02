"""Job links on Greenhouse, Ashby and Lever are read from the board's public API,
not the page: a closed job's page redirects to the board, which would otherwise
be captured as the job (ADR 0007). Other links are refused if a redirect moved
them to another path."""

import html
import json

import httpx
import pytest
from google.cloud import storage

from jhunt.jobs.boards import BoardJob, board_job, embedded_job_id
from jhunt.platform.artifacts import RecordStore
from jhunt.workflows.capture.snapshot import FetchFailed, snapshot_posting_tool
from tests.runtime.local_site import Site

KEY = "b" * 64
DESCRIPTION = (
    "<h2>About the role</h2><p>Build durable workflows and own the Postgres schema. "
    + "You will work closely with product and design on what to build next. " * 8
    + "</p><ul><li>Python</li><li>Postgres</li></ul>"
)


def tool_for(site: Site, store: RecordStore, http: httpx.Client, prefix: str):  # type: ignore[no-untyped-def]
    # The board APIs are served by the local site in tests.
    apis = {"greenhouse": site.base, "ashby": site.base, "lever": site.base}
    return snapshot_posting_tool(store, http, prefix=prefix, apis=apis)


def stored(bucket: storage.Bucket, prefix: str) -> list[str]:
    return [b.name for b in bucket.client.list_blobs(bucket, prefix=prefix)]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/4567890",
            BoardJob("greenhouse", "acme", "4567890"),
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/123?gh_src=x",
            BoardJob("greenhouse", "acme", "123"),
        ),
        (
            "https://jobs.ashbyhq.com/wayflyer/0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            BoardJob("ashby", "wayflyer", "0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"),
        ),
        (
            "https://jobs.ashbyhq.com/wayflyer/0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b/application",
            BoardJob("ashby", "wayflyer", "0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"),
        ),
        (
            "https://jobs.lever.co/acme/5f1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            BoardJob("lever", "acme", "5f1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"),
        ),
        ("https://job-boards.greenhouse.io/acme", None),  # the board, not a job
        ("https://jobs.ashbyhq.com/wayflyer", None),
        ("https://stripe.com/jobs/listing/x/123", None),  # not a board host
    ],
)
def test_board_job_links_are_recognised(url: str, expected: BoardJob | None) -> None:
    assert board_job(url) == expected


def test_a_valid_greenhouse_job_is_read_from_the_api(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    # Greenhouse returns the description HTML entity-escaped in "content".
    api = {"id": 123, "title": "Engineer", "content": html.escape(DESCRIPTION)}
    site.pages["v1/boards/acme/jobs/123?content=true"] = (200, json.dumps(api).encode())

    result = tool_for(site, store, http, record_prefix).run(
        {"url": "https://job-boards.greenhouse.io/acme/jobs/123", "dedupe_key": KEY}
    )

    kept = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    assert kept == DESCRIPTION.encode()
    assert site.hits == ["v1/boards/acme/jobs/123?content=true"]  # the API only, never the page


def test_a_missing_greenhouse_job_is_refused(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_bucket: storage.Bucket,
    record_prefix: str,
) -> None:
    # The real bug: jobs/4567890 didn't exist, its page redirected to the board.
    with pytest.raises(FetchFailed, match="job not found"):
        tool_for(site, store, http, record_prefix).run(
            {"url": "https://job-boards.greenhouse.io/acme/jobs/4567890", "dedupe_key": KEY}
        )
    assert stored(gcs_bucket, record_prefix) == []


def test_an_ashby_job_is_read_from_the_posting_api(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_bucket: storage.Bucket,
    record_prefix: str,
) -> None:
    job_id = "0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    board = {
        "jobs": [
            {"id": "other", "descriptionHtml": "<p>x</p>"},
            {"id": job_id, "descriptionHtml": DESCRIPTION},
        ]
    }
    site.pages["posting-api/job-board/wayflyer"] = (200, json.dumps(board).encode())
    tool = tool_for(site, store, http, record_prefix)

    result = tool.run({"url": f"https://jobs.ashbyhq.com/wayflyer/{job_id}", "dedupe_key": KEY})
    kept = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    assert kept == DESCRIPTION.encode()

    with pytest.raises(FetchFailed, match="job not found"):
        tool.run(
            {
                "url": f"https://jobs.ashbyhq.com/wayflyer/{'c' * 8}-0000-0000-0000-{'0' * 12}",
                "dedupe_key": "c" * 64,
            }
        )


def test_a_lever_job_is_read_from_the_postings_api(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    job_id = "5f1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    posting = {
        "description": DESCRIPTION,
        "lists": [{"text": "What you'll do", "content": "<li>Ship</li>"}],
        "additional": "<p>Hybrid, Dublin.</p>",
    }
    site.pages[f"v0/postings/acme/{job_id}"] = (200, json.dumps(posting).encode())
    tool = tool_for(site, store, http, record_prefix)

    result = tool.run({"url": f"https://jobs.lever.co/acme/{job_id}", "dedupe_key": KEY})
    kept = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    assert (
        kept
        == (
            DESCRIPTION + "<h3>What you'll do</h3><ul><li>Ship</li></ul><p>Hybrid, Dublin.</p>"
        ).encode()
    )

    with pytest.raises(FetchFailed, match="job not found"):
        tool.run(
            {
                "url": f"https://jobs.lever.co/acme/{'d' * 8}-0000-0000-0000-{'0' * 12}",
                "dedupe_key": "d" * 64,
            }
        )


def test_a_redirect_to_another_path_is_refused(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_bucket: storage.Bucket,
    record_prefix: str,
) -> None:
    long_page = f"<html><body>{DESCRIPTION}</body></html>".encode()
    site.pages["careers"] = (200, long_page)  # the careers page passes the 500-char guard
    site.redirects["careers/jobs/987"] = "careers"
    with pytest.raises(
        FetchFailed,
        match=r"redirected from .*/careers/jobs/987 to .*/careers; the job may have closed",
    ):
        tool_for(site, store, http, record_prefix).run(
            {"url": site.url("careers/jobs/987"), "dedupe_key": KEY}
        )
    assert stored(gcs_bucket, record_prefix) == []


def test_a_redirect_that_keeps_the_path_is_fine(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    long_page = f"<html><body>{DESCRIPTION}</body></html>".encode()
    site.pages["careers/jobs/987/"] = (200, long_page)
    site.redirects["careers/jobs/987"] = "careers/jobs/987/"  # only a trailing slash added
    result = tool_for(site, store, http, record_prefix).run(
        {"url": site.url("careers/jobs/987"), "dedupe_key": KEY}
    )
    assert result["html"]["sha256"]


# --- boards embedded in a company's own careers page ------------------------

ASHBY_JID = "5635dd7e-a0ca-414d-b738-71500502dba9"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.example/careers?gh_jid=4012345", ("greenhouse", "4012345")),
        (f"https://adaptionlabs.ai/careers?ashby_jid={ASHBY_JID}", ("ashby", ASHBY_JID)),
        ("https://acme.example/careers?gh_jid=abc", None),  # not a Greenhouse id
        ("https://acme.example/careers?ashby_jid=123", None),  # not a uuid
        ("https://acme.example/careers", None),
    ],
)
def test_embedded_job_ids_are_recognised(url: str, expected: tuple[str, str] | None) -> None:
    assert embedded_job_id(url) == expected


@pytest.mark.parametrize(
    "embed",
    [
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        '<a href="https://job-boards.greenhouse.io/acme/jobs/4012345">Apply</a>',
    ],
)
def test_an_embedded_greenhouse_board_is_read_from_its_api(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str, embed: str
) -> None:
    site.pages["careers?gh_jid=4012345"] = (200, f"<html><body>{embed}</body></html>".encode())
    api = {"id": 4012345, "content": html.escape(DESCRIPTION)}
    site.pages["v1/boards/acme/jobs/4012345?content=true"] = (200, json.dumps(api).encode())

    result = tool_for(site, store, http, record_prefix).run(
        {"url": site.url("careers?gh_jid=4012345"), "dedupe_key": KEY}
    )

    kept = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    assert kept == DESCRIPTION.encode()  # the job's description, not the careers page
    assert site.hits == ["careers?gh_jid=4012345", "v1/boards/acme/jobs/4012345?content=true"]


def test_an_embedded_ashby_board_is_read_from_its_api(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    # The real case: adaptionlabs.ai/careers?ashby_jid=... embeds jobs.ashbyhq.com/<org>.
    page = '<html><body><script src="https://jobs.ashbyhq.com/adaption/embed?version=2"></script></body></html>'
    site.pages[f"careers?ashby_jid={ASHBY_JID}"] = (200, page.encode())
    board = {"jobs": [{"id": ASHBY_JID, "descriptionHtml": DESCRIPTION}]}
    site.pages["posting-api/job-board/adaption"] = (200, json.dumps(board).encode())

    result = tool_for(site, store, http, record_prefix).run(
        {"url": site.url(f"careers?ashby_jid={ASHBY_JID}"), "dedupe_key": KEY}
    )

    kept = store.get(
        f"{record_prefix}{KEY}/page.html", result["html"]["generation"], result["html"]["sha256"]
    )
    assert kept == DESCRIPTION.encode()


def test_an_embedded_job_whose_board_cant_be_found_is_refused(
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    gcs_bucket: storage.Bucket,
    record_prefix: str,
) -> None:
    # A careers page long enough to pass the page guard, with no board in it:
    # it must not be kept as the job (no fallback to the page).
    site.pages[f"careers?ashby_jid={ASHBY_JID}"] = (
        200,
        f"<html><body>{DESCRIPTION}</body></html>".encode(),
    )
    with pytest.raises(FetchFailed, match="embedded board not found"):
        tool_for(site, store, http, record_prefix).run(
            {"url": site.url(f"careers?ashby_jid={ASHBY_JID}"), "dedupe_key": KEY}
        )
    assert stored(gcs_bucket, record_prefix) == []


def test_a_page_naming_two_different_boards_is_refused(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    page = (
        '<a href="https://jobs.ashbyhq.com/adaption/x">one</a>'
        '<a href="https://jobs.ashbyhq.com/partner/y">two</a>'
    )
    site.pages[f"careers?ashby_jid={ASHBY_JID}"] = (200, page.encode())
    with pytest.raises(FetchFailed, match="embedded board not found"):
        tool_for(site, store, http, record_prefix).run(
            {"url": site.url(f"careers?ashby_jid={ASHBY_JID}"), "dedupe_key": KEY}
        )


# --- the board's own title and company ------------------------------------


def test_a_board_job_keeps_its_title_and_company_and_a_retry_reads_them_back(
    site: Site, store: RecordStore, http: httpx.Client, record_prefix: str
) -> None:
    api = {
        "title": "Platform Engineer",
        "company_name": "Acme Inc",
        "content": html.escape(DESCRIPTION),
    }
    site.pages["v1/boards/acme/jobs/123?content=true"] = (200, json.dumps(api).encode())
    tool = tool_for(site, store, http, record_prefix)
    args = {"url": "https://job-boards.greenhouse.io/acme/jobs/123", "dedupe_key": KEY}

    first = tool.run(args)
    expected = {
        "provider": "greenhouse",
        "board": "acme",
        "job_id": "123",
        "title": "Platform Engineer",
        "company": "Acme Inc",
    }
    assert first["job"] == expected

    # A retry adopts the kept page without fetching, and still has the details.
    site.hits.clear()
    assert tool.run(args) == first
    assert site.hits == []
