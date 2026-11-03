"""The digest's Scout panel and live run status (ADR 0017, U2): Run Scout now,
Retry after a failure, Rescore after a success, the same rules as jhunt-scout;
anything that queues a run updates in place from GET /runs/<id>."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.worker import process_run
from jhunt.workflows import capture, scout
from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings
from tests.runtime.lease_watch import LeaseWatch

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
DAY = "2026-11-03"
POST = {"headers": {"Origin": ORIGIN}, "follow_redirects": False}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """The repo's scope and targets, and no boards: a run fetches nothing."""
    repo = Path(__file__).resolve().parents[2] / "config"
    for name in ("scope.toml", "targets.toml", "claim_levels.toml", "pack.toml"):
        (tmp_path / name).write_text((repo / name).read_text())
    (tmp_path / "sources.toml").write_text("sources = []\n")
    return tmp_path


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()


@pytest.fixture
def registry(db: tuple[Engine, str]) -> Registry:
    engine, _ = db
    gateway = ToolGateway(engine, [])
    registry = Registry()
    registry.register(capture.WORKFLOW, capture.VERSION, capture.build(engine, gateway))
    for version in scout.VERSIONS:
        registry.register(scout.WORKFLOW, version, scout.build(engine, gateway))
    return registry


@pytest.fixture
def client(db: tuple[Engine, str], registry: Registry, config_dir: Path) -> Iterator[TestClient]:
    engine, info = db
    app = create_app(
        engine, registry, port=PORT, config_dir=config_dir, conninfo=info, today=lambda: DAY
    )
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def scout_runs(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT id, thread_id, status FROM runs WHERE workflow = 'scout' ORDER BY id")
            )
        )


def close(engine: Engine, run_id: int, status: str, error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = :s, error = :e WHERE id = :i"),
            {"s": status, "e": error, "i": run_id},
        )


def panel(client: TestClient) -> str:
    page = client.get("/").text
    return page[page.index('<section id="scout"') : page.index("</section>")]


def test_run_scout_now_queues_todays_run_once_and_shows_it_live(
    db: tuple[Engine, str], client: TestClient
) -> None:
    engine, _ = db
    assert 'action="/scout/run"' in panel(client)

    response = client.post("/scout/run", **POST)
    assert (response.status_code, response.headers["location"]) == (303, "/#scout")
    [run] = scout_runs(engine)
    assert run.thread_id == f"scout-{DAY}"

    shown = panel(client)
    assert f'data-run="{run.id}"' in shown and ">queued<" in shown  # updated in place from here
    assert 'action="/scout/run"' not in shown
    page = client.get("/").text
    assert "fetch(" in page and '<noscript><meta http-equiv="refresh"' in page

    client.post("/scout/run", **POST)  # the same rule as jhunt-scout: once a day
    assert len(scout_runs(engine)) == 1


def test_a_failed_run_shows_its_error_and_offers_retry(
    db: tuple[Engine, str], client: TestClient
) -> None:
    engine, _ = db
    client.post("/scout/run", **POST)
    [run] = scout_runs(engine)
    close(engine, run.id, "failed", "ToolRefused: <level 0>")

    shown = panel(client)
    assert "ToolRefused: &lt;level 0&gt;" in shown
    assert 'action="/scout/retry"' in shown and 'action="/scout/rescore"' not in shown
    client.post("/scout/retry", **POST)
    assert [r.thread_id for r in scout_runs(engine)] == [f"scout-{DAY}", f"scout-{DAY}-2"]
    client.post("/scout/retry", **POST)  # the retry is queued, not failed: no third
    assert len(scout_runs(engine)) == 2


def test_a_finished_run_shows_its_summary_and_offers_rescore(
    db: tuple[Engine, str], client: TestClient, registry: Registry, lease_watch: LeaseWatch
) -> None:
    engine, info = db
    client.post("/scout/run", **POST)
    claimed = queue.claim(engine)
    assert claimed is not None
    process_run(engine, info, registry, claimed, on_lease_lost=lease_watch)  # no boards to fetch

    shown = panel(client)
    assert f"scout {DAY}: 0 boards, 0 failed" in shown
    assert 'action="/scout/rescore"' in shown and 'action="/scout/retry"' not in shown
    assert "<noscript>" not in client.get("/").text  # nothing left to wait for
    client.post("/scout/rescore", **POST)
    assert [r.thread_id for r in scout_runs(engine)][-1] == f"scout-{DAY}-2"


def test_the_run_status_endpoint(db: tuple[Engine, str], client: TestClient) -> None:
    engine, _ = db
    client.post("/scout/run", **POST)
    [run] = scout_runs(engine)
    assert client.get(f"/runs/{run.id}").json() == {"status": "queued", "text": "queued"}
    close(engine, run.id, "running")
    assert client.get(f"/runs/{run.id}").json()["text"] == "running"
    close(engine, run.id, "failed", "storage down")
    assert client.get(f"/runs/{run.id}").json() == {
        "status": "failed",
        "text": "failed: storage down",
    }
    assert client.get("/runs/999999").status_code == 404


def test_a_queued_capture_updates_in_place_too(db: tuple[Engine, str], client: TestClient) -> None:
    engine, _ = db
    upsert_postings(engine, [Posting(
        dedupe_key=capture.capture_key("https://job-boards.greenhouse.io/acme/jobs/1"),
        provider="greenhouse", board="acme", job_id="1",
        url="https://job-boards.greenhouse.io/acme/jobs/1", title="ML Engineer", company="Acme",
        location="Dublin", remote=False, in_scope=True, scope_reason="Dublin",
        description_html="", fit_score=0.6, fit_matches={"title": ["ml engineer"]},
    )])  # fmt: skip
    with engine.connect() as conn:
        posting_id = conn.execute(text("SELECT id FROM postings")).scalar_one()
    client.post(f"/postings/{posting_id}/capture", **POST)
    with engine.connect() as conn:
        run_id = conn.execute(text("SELECT id FROM runs WHERE workflow = 'capture'")).scalar_one()
    row = client.get("/").text
    row = row[row.index(f'id="posting-{posting_id}"') :]
    assert re.search(rf'data-run="{run_id}"[^>]*>Capture queued<', row)


@pytest.mark.parametrize("path", ["/scout/run", "/scout/retry", "/scout/rescore"])
def test_scout_buttons_need_this_pages_origin(
    db: tuple[Engine, str], client: TestClient, path: str
) -> None:
    engine, _ = db
    assert client.post(path, follow_redirects=False).status_code == 403
    assert scout_runs(engine) == []


def test_a_bad_config_shows_the_problem_and_queues_nothing(
    db: tuple[Engine, str], client: TestClient, config_dir: Path
) -> None:
    engine, _ = db
    (config_dir / "sources.toml").write_text("[other]\n")
    client.post("/scout/run", **POST)
    assert scout_runs(engine) == []
    assert "must contain exactly one list, [[sources]]" in panel(client)
