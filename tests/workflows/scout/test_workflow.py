"""The Scout workflow end to end, in-process: each board's list is fetched into
staging and its postings stored with their scope; a board that fails is listed,
not fatal; postings unseen for 30 days are removed unless captured (ADR 0011)."""

from typing import Any

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy import Engine, text

from jhunt.platform.artifacts import StagingStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import enqueue_start
from jhunt.platform.runtime.worker import process_run
from jhunt.workflows import scout
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.scout.location import Scope
from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.scout.sources import Source
from tests.runtime.lease_watch import LeaseWatch
from tests.runtime.local_site import Site
from tests.workflows.scout.fixtures import SOURCES, serve_boards
from tests.workflows.scout.test_store_postings import TARGETS

DAY = "2026-11-01"


def wired(engine: Engine, staging: StagingStore, http: httpx.Client, site: Site, prefix: str):  # type: ignore[no-untyped-def]
    """What jhunt.app wires in production, with the local site as both APIs."""
    apis = {"greenhouse": site.base, "ashby": site.base}
    gateway = ToolGateway(
        engine,
        [
            scout.fetch_board_tool(staging, http, prefix=prefix, apis=apis),
            scout.store_postings_tool(engine, staging),
        ],
    )
    registry = Registry()
    registry.register(scout.WORKFLOW, scout.VERSION, scout.build(engine, gateway))
    return registry


def run_scout(
    db: tuple[Engine, str], registry: Registry, sources: list[Source], lease_watch: LeaseWatch
) -> dict[str, Any]:
    """Queue and process one Scout run; return its run status and final state."""
    engine, info = db
    thread_id = scout.thread_id_for(DAY)
    graph_input = scout.scout_input(DAY, sources, Scope(northern_ireland_in_scope=False), TARGETS)
    run_id = enqueue_start(engine, registry, thread_id, scout.WORKFLOW, graph_input)
    run = queue.claim(engine)
    assert run is not None and run.id == run_id
    process_run(engine, info, registry, run, on_lease_lost=lease_watch)
    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
        assert status.scalar_one() == "succeeded"
    builder = registry.get(scout.WORKFLOW, scout.VERSION)
    assert builder is not None
    with Connection.connect(info, autocommit=True, row_factory=dict_row) as pg:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return dict(builder(PostgresSaver(pg)).get_state(config).values)


def test_scout_stores_every_posting_and_lists_the_board_that_failed(
    db: tuple[Engine, str],
    site: Site,
    staging: StagingStore,
    http: httpx.Client,
    record_prefix: str,
    lease_watch: LeaseWatch,
) -> None:
    engine, _ = db
    serve_boards(site.pages)
    state = run_scout(db, wired(engine, staging, http, site, record_prefix), SOURCES, lease_watch)

    with engine.connect() as conn:
        postings = conn.execute(
            text(
                "SELECT board, job_id, in_scope, scope_reason, fit_score FROM postings"
                " ORDER BY job_id"
            )
        ).all()
        calls = conn.execute(text("SELECT tool, step_key FROM tool_calls ORDER BY id")).all()
    assert [tuple(p) for p in postings] == [
        ("acme", "101", True, "Dublin", pytest.approx(0.68)),
        ("acme", "102", False, "out: San Francisco, CA", 0.0),
        ("beta", "ab12", True, "region EMEA", 0.0),
    ]
    assert [tuple(c) for c in calls] == [
        ("fetch_board", "greenhouse:acme"),
        ("fetch_board", "ashby:beta"),
        ("store_postings", "greenhouse:acme"),
        ("store_postings", "ashby:beta"),
    ]
    assert list(state["failed"]) == ["ashby:gone"]
    assert "board not found" in state["failed"]["ashby:gone"]
    assert scout.summary(state) == [
        f"scout {DAY}: 3 boards, 1 failed",
        "postings: 3 first seen in this run, 0 seen again, 2 in scope",
        "removed: 0 unseen for 30 days",
        "failed: ashby:gone: " + state["failed"]["ashby:gone"],
    ]


def posting(job_id: str) -> Posting:
    return Posting(
        dedupe_key=f"key-{job_id}",
        provider="greenhouse",
        board="old",
        job_id=job_id,
        url=f"https://job-boards.greenhouse.io/old/jobs/{job_id}",
        title="Engineer",
        company="Old",
        location="Dublin",
        remote=False,
        in_scope=True,
        scope_reason="Dublin",
        description_html="",
        fit_score=None,
        fit_matches=None,
    )


def test_postings_unseen_for_30_days_are_removed_unless_captured(
    db: tuple[Engine, str],
    site: Site,
    staging: StagingStore,
    http: httpx.Client,
    record_prefix: str,
    lease_watch: LeaseWatch,
) -> None:
    engine, _ = db
    upsert_postings(engine, [posting(j) for j in ("1", "2", "3")])
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE postings SET last_seen_at = now() - CASE job_id"
                " WHEN '1' THEN interval '31 days'"  # unseen: removed
                " WHEN '2' THEN interval '31 days'"  # unseen but captured: kept
                " ELSE interval '29 days' END"  # seen within 30 days: kept
            )
        )
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    record_capture(
        engine,
        url=posting("2").url,
        company="Old",
        title="Engineer",
        dedupe_key="key-2",
        snapshot=refs,
    )

    state = run_scout(db, wired(engine, staging, http, site, record_prefix), [], lease_watch)

    with engine.connect() as conn:
        left = conn.execute(text("SELECT job_id FROM postings ORDER BY job_id")).scalars().all()
    assert left == ["2", "3"]
    assert state["removed"] == 1
