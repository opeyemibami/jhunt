"""The Capture workflow end to end, in-process: a worker runs it, the snapshot
is kept in records, and the application, snapshot row and event are recorded."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import enqueue_start
from jhunt.platform.runtime.worker import process_run
from jhunt.workflows import capture
from jhunt.workflows.capture.dedupe import dedupe_key
from jhunt.workflows.capture.record import record_capture
from tests.runtime.lease_watch import LeaseWatch
from tests.runtime.local_site import Site

ABOUT = (
    "Join the platform team to run durable workflows in production. You will own the job "
    "queue, the storage layer and the guarantees around them, with a lot of autonomy. "
) * 4
PAGE = (
    f"<html><body><h1>Platform Engineer</h1><p>Dublin, Ireland</p><p>{ABOUT}</p></body></html>"
).encode()


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()


def wired(engine: Engine, store: RecordStore, http: httpx.Client, prefix: str) -> Registry:
    """What jhunt.app wires in production, with the test's storage prefix."""
    gateway = ToolGateway(engine, [capture.snapshot_posting_tool(store, http, prefix=prefix)])
    registry = Registry()
    registry.register(capture.WORKFLOW, capture.VERSION, capture.build(engine, gateway))
    return registry


def rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
        ]


def start(engine: Engine, registry: Registry, url: str) -> tuple[str, int]:
    key = dedupe_key(url, "Acme", "Platform Engineer")
    thread_id = capture.thread_id_for(key)
    run_id = enqueue_start(
        engine,
        registry,
        thread_id,
        capture.WORKFLOW,
        {"url": url, "company": "Acme", "title": "Platform Engineer", "dedupe_key": key},
    )
    return key, run_id


def test_capture_keeps_the_page_and_opens_the_application(
    db: tuple[Engine, str],
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    record_prefix: str,
    lease_watch: LeaseWatch,
) -> None:
    engine, info = db
    site.pages["jobs/42"] = (200, PAGE)
    registry = wired(engine, store, http, record_prefix)
    key, run_id = start(engine, registry, site.url("jobs/42"))

    run = queue.claim(engine)
    assert run is not None and run.id == run_id
    process_run(engine, info, registry, run, on_lease_lost=lease_watch)

    [app] = rows(engine, "applications")
    [snap] = rows(engine, "jd_snapshots")
    [event] = rows(engine, "events")
    [call] = rows(engine, "tool_calls")
    assert (app["dedupe_key"], app["company"], app["role"], app["stage"]) == (
        key,
        "Acme",
        "Platform Engineer",
        "captured",
    )
    assert (app["snapshot_id"], snap["url"]) == (snap["id"], site.url("jobs/42"))
    assert (event["application_id"], event["type"], event["actor"]) == (
        app["id"],
        "captured",
        "workflow",
    )
    assert (call["tool"], call["run_id"], call["thread_id"]) == (
        "snapshot_posting",
        run_id,
        capture.thread_id_for(key),
    )
    # The snapshot row points at exactly the bytes that were kept.
    html_key = snap["html_uri"].split("/", 3)[3]
    text_key = snap["text_uri"].split("/", 3)[3]
    assert store.get(html_key, snap["html_generation"], snap["html_sha256"]) == PAGE
    assert store.get(text_key, snap["text_generation"], snap["text_sha256"]) == (
        f"Platform Engineer\nDublin, Ireland\n{ABOUT.strip()}".encode()
    )
    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
        ).scalar()
    assert status == "succeeded"


def test_recording_twice_makes_one_application_and_one_snapshot(db: tuple[Engine, str]) -> None:
    # A re-run of the record step (after a crash) must not add rows: snapshot
    # rows can never be deleted, so a stray one would stay for good.
    engine, _ = db
    refs = {
        "html": {"uri": "gs://b/k/page.html", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/k/page.txt", "generation": 2, "sha256": "bb"},
    }
    args = {"url": "https://x/1", "company": "Acme", "title": "Engineer", "dedupe_key": "k" * 64}
    first = record_capture(engine, snapshot=refs, **args)
    second = record_capture(engine, snapshot=refs, **args)

    assert first == (first[0], False)
    assert second == (first[0], True)  # already captured: the same application
    assert len(rows(engine, "applications")) == 1
    assert len(rows(engine, "jd_snapshots")) == 1
    assert len(rows(engine, "events")) == 1


@pytest.mark.parametrize(
    ("url", "api_path", "body", "company"),
    [
        (  # Greenhouse names the company
            "https://job-boards.greenhouse.io/acme/jobs/77",
            "v1/boards/acme/jobs/77?content=true",
            {"title": "Data Engineer", "company_name": "Acme Inc", "content": "CONTENT"},
            "Acme Inc",
        ),
        (  # Ashby doesn't: the board's slug stands in
            "https://jobs.ashbyhq.com/acme/0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            "posting-api/job-board/acme",
            {
                "jobs": [
                    {
                        "id": "0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
                        "title": "Data Engineer",
                        "descriptionHtml": "CONTENT",
                    }
                ]
            },
            "acme",
        ),
    ],
)
def test_a_board_job_is_captured_without_typing_company_or_title(
    db: tuple[Engine, str],
    site: Site,
    store: RecordStore,
    http: httpx.Client,
    record_prefix: str,
    lease_watch: LeaseWatch,
    url: str,
    api_path: str,
    body: dict[str, Any],
    company: str,
) -> None:
    engine, info = db
    description = "<p>" + "Build data pipelines in Python and Postgres. " * 15 + "</p>"
    site.pages[api_path] = (200, json.dumps(body).replace("CONTENT", description).encode())
    gateway = ToolGateway(
        engine,
        [
            capture.snapshot_posting_tool(
                store,
                http,
                prefix=record_prefix,
                apis={"greenhouse": site.base, "ashby": site.base, "lever": site.base},
            )
        ],
    )
    registry = Registry()
    registry.register(capture.WORKFLOW, capture.VERSION, capture.build(engine, gateway))
    key = capture.capture_key(url)  # no company or title typed
    enqueue_start(
        engine, registry, capture.thread_id_for(key), capture.WORKFLOW,
        {"url": url, "company": None, "title": None, "dedupe_key": key},
    )  # fmt: skip
    run = queue.claim(engine)
    assert run is not None
    process_run(engine, info, registry, run, on_lease_lost=lease_watch)

    [app] = rows(engine, "applications")
    assert (app["company"], app["role"], app["dedupe_key"]) == (company, "Data Engineer", key)
