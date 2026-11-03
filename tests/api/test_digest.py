"""The digest page (ADR 0012): in-scope postings with a title match first, by
score, with what matched; the rest collapsed; out-of-scope ones counted; a
Capture button per posting that queues a Capture run from the board API."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.jobs.dedupe import board_job_key
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows import capture
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings
from jhunt.workflows.workspace.submission import SubmittedFile, record_submission

PORT = 8765
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def app_for(engine: Engine, registry: Registry):  # type: ignore[no-untyped-def]
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    return create_app(engine, registry, port=PORT, config_dir=REPO_CONFIG, conninfo=conninfo,
                      records=RecordStore("unused"))  # fmt: skip


ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    registry = Registry()
    registry.register(
        capture.WORKFLOW, capture.VERSION, capture.build(engine, ToolGateway(engine, []))
    )
    with TestClient(app_for(engine, registry), base_url=ORIGIN) as c:
        yield c


def posting(
    job_id: str,
    title: str,
    score: float | None,
    matches: dict[str, list[str]] | None,
    *,
    in_scope: bool = True,
    url: str | None = None,
    provider: str = "greenhouse",
) -> Posting:
    return Posting(
        dedupe_key=board_job_key(provider, job_id),
        provider=provider,
        board="acme",
        job_id=job_id,
        url=url or f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        title=title,
        company="Acme",
        location="Dublin" if in_scope else "London",
        remote=False,
        in_scope=in_scope,
        scope_reason="Dublin" if in_scope else "out: London",
        description_html="",
        fit_score=score,
        fit_matches=matches,
    )


POSTINGS = [
    posting("1", "ML Engineer", 0.82, {"title": ["ml engineer"], "include": ["llm", "python"]}),
    posting("2", "Forward Deployed Engineer", 1.0,
            {"title": ["forward deployed"], "include": ["agents"]}),
    posting("3", "Data Scientist", 0.5, {"include": ["llm", "evals"]}),
    posting("4", "Office Manager", 0.0, {"exclude": ["manager"]}),
    posting("5", "Staff ML Engineer", 0.2, {"title": ["ml engineer"], "penalty": ["staff ml"]}),
    posting("6", "Recruiter, London", 0.0, {"exclude": ["recruiter"]}, in_scope=False),
    posting("7", "Engineer, San Francisco", 0.9, {"title": ["ml engineer"]}, in_scope=False),
    # First by title, not by score: the order must come from the score
    posting("10", "AI Engineer", 0.6, {"title": ["ai engineer"]}),
    posting("11", "Analyst", 0.1, {"include": ["python"]}),
]  # fmt: skip


def page(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def titles(html: str) -> list[str]:
    return re.findall(r'class="title"[^>]*>([^<]+)<', html)


def id_of(engine: Engine, job_id: str) -> int:
    with engine.connect() as conn:
        found: int = conn.execute(
            text("SELECT id FROM postings WHERE job_id = :j"), {"j": job_id}
        ).scalar_one()
    return found


def capture_runs(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT thread_id, workflow, payload FROM runs WHERE workflow = 'capture'")
            )
        )


def test_title_matches_come_first_by_score_and_the_rest_are_collapsed(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, POSTINGS)
    html = page(client)

    matched, other = html.split("<details", 1)
    assert titles(matched) == [
        "Forward Deployed Engineer",
        "ML Engineer",
        "AI Engineer",
        "Staff ML Engineer",
    ]
    assert titles(other) == ["Data Scientist", "Analyst", "Office Manager"]
    assert "Other in-scope jobs (3)" in other
    assert "<details open" not in html  # collapsed
    # Out-of-scope postings: counted, never listed
    assert "2 out-of-scope postings hidden" in html
    assert "Recruiter" not in html and "San Francisco" not in html


def test_each_posting_shows_its_score_and_what_matched(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, POSTINGS)
    html = page(client)
    assert "0.82" in html
    assert "title: ml engineer" in html
    assert "include: llm, python" in html
    assert "penalty: staff ml" in html
    assert "exclude: manager" in html
    assert 'href="https://job-boards.greenhouse.io/acme/jobs/2"' in html


def test_unscored_postings_are_listed_last(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting("8", "Unscored Job", None, None), *POSTINGS[2:4]])
    _, other = page(client).split("<details", 1)
    assert titles(other) == ["Data Scientist", "Office Manager", "Unscored Job"]


def test_board_text_is_escaped_and_only_web_links_are_links(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(
        engine,
        [
            posting("9", "<script>alert(1)</script>", 0.5, {"include": ["<b>x</b>"]},
                    url="javascript:alert(1)"),
        ],
    )  # fmt: skip
    html = page(client)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "javascript:" not in html


def test_capture_queues_a_capture_run_from_the_board_once(
    engine: Engine, client: TestClient
) -> None:
    ashby_id = "5635dd7e-a0ca-414d-b738-71500502dba9"
    upsert_postings(
        engine,
        [posting(ashby_id, "ML Engineer", 0.9, {"title": ["ml engineer"]}, provider="ashby",
                 url="https://careers.acme.example/jobs?ashby_jid=" + ashby_id)],
    )  # fmt: skip
    posting_id = id_of(engine, ashby_id)
    key = board_job_key("ashby", ashby_id)

    response = client.post(
        f"/postings/{posting_id}/capture", headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/#posting-{posting_id}"
    [run] = capture_runs(engine)
    assert run.thread_id == capture.thread_id_for(key)
    # The board's own link, not the careers page: Capture reads the board API,
    # under the posting's key. The posting's company and title fill the gaps
    # (Ashby doesn't name the company).
    assert run.payload == {
        "url": f"https://jobs.ashbyhq.com/acme/{ashby_id}",
        "company": "Acme",
        "title": "ML Engineer",
        "dedupe_key": key,
    }
    assert "Capture queued" in page(client)

    # Asking again queues nothing more.
    client.post(
        f"/postings/{posting_id}/capture", headers={"Origin": ORIGIN}, follow_redirects=False
    )
    assert len(capture_runs(engine)) == 1


def test_a_captured_posting_links_its_application_instead_of_a_button(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, POSTINGS[:1])
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine,
        url=POSTINGS[0].url,
        company="Acme",
        title="ML Engineer",
        dedupe_key=POSTINGS[0].dedupe_key,
        snapshot=refs,
    )
    html = page(client)
    link = f'<a href="/applications/{application_id}">application {application_id}</a>'
    assert f"Captured: {link}" in html
    row = html[html.index('id="posting-') :]  # the posting, not the Scout panel
    assert "<button" not in row


def test_a_submitted_application_shows_its_latest_submission(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, POSTINGS[:1])
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url=POSTINGS[0].url, company="Acme", title="ML Engineer",
        dedupe_key=POSTINGS[0].dedupe_key, snapshot=refs,
    )  # fmt: skip
    cv = SubmittedFile("cv", "cv.pdf", "application/pdf", 3, {
        "uri": "gs://b/c", "generation": 1, "sha256": "1" * 64})  # fmt: skip
    for key, day, channel in (("k1", "2026-11-01", "email"), ("k2", "2026-11-03", "<site>")):
        record_submission(
            engine, submission_key=key, application_id=application_id, channel=channel,
            submitted_at=f"{day}T09:00:00+00:00", files=[cv],
        )  # fmt: skip
    html = page(client)
    link = f'<a href="/applications/{application_id}">application {application_id}</a>'
    assert f"Captured: {link} · Submitted 2026-11-03 via &lt;site&gt;" in html
    assert "2026-11-01" not in html


def test_a_failed_capture_shows_its_error(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, POSTINGS[:1])
    posting_id = id_of(engine, "1")
    client.post(
        f"/postings/{posting_id}/capture", headers={"Origin": ORIGIN}, follow_redirects=False
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed', error = 'JobNotFound: <gone>'"))
    html = page(client)
    assert "Capture failed: JobNotFound: &lt;gone&gt;" in html


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}],
)
def test_capture_from_another_origin_is_refused(
    engine: Engine, client: TestClient, headers: dict[str, str]
) -> None:
    upsert_postings(engine, POSTINGS[:1])
    response = client.post(
        f"/postings/{id_of(engine, '1')}/capture", headers=headers, follow_redirects=False
    )
    assert response.status_code == 403
    assert capture_runs(engine) == []


def test_another_host_name_is_refused(engine: Engine) -> None:
    # A DNS name pointed at 127.0.0.1 (DNS rebinding) must not read the page.
    registry = Registry()
    with TestClient(app_for(engine, registry), base_url=f"http://evil.example:{PORT}") as c:
        assert c.get("/").status_code == 400
    with TestClient(app_for(engine, registry), base_url=f"http://localhost:{PORT}") as c:
        assert c.get("/").status_code == 200


def test_capturing_an_unknown_or_out_of_scope_posting_is_refused(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, POSTINGS[5:6])  # out of scope
    for posting_id in (id_of(engine, "6"), 999999):
        response = client.post(
            f"/postings/{posting_id}/capture", headers={"Origin": ORIGIN}, follow_redirects=False
        )
        assert response.status_code == 404
    assert capture_runs(engine) == []
