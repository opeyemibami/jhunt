"""The applications list and an application's page (ADR 0017, U3): its stored
job description, stage, events, submissions and drafts.

The job description is read from records directly, by exact generation with
its sha256 checked: the read-only exception in ADR 0017. It's stored in the
real CI bucket here, like every storage test.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.cloud import storage
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.memory.claims import load_claim_levels
from jhunt.platform.memory.facts import FactVersion
from jhunt.platform.models import CallRecord, ModelRequest, ModelResponse
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows.application_pack.pack import draft_pack, store_draft
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.workspace.submission import SubmittedFile, record_submission

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
JOB = "AI Engineer at Fin.\n\nYou'll build <agents> & evals.\nRequirements: Python."


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def records(gcs_test_bucket: str, gcs_client: storage.Client) -> RecordStore:
    return RecordStore(gcs_test_bucket, gcs_client)


@pytest.fixture
def client(engine: Engine, records: RecordStore) -> Iterator[TestClient]:
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app = create_app(engine, Registry(), port=PORT, config_dir=REPO_CONFIG, conninfo=conninfo,
                     records=records)  # fmt: skip
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def capture(engine: Engine, records: RecordStore, prefix: str, *, company: str = "Fin",
            role: str = "AI Engineer", n: int = 1, job: str = JOB,
            sha256: str | None = None) -> int:  # fmt: skip
    page = records.put(f"{prefix}{n}/page.txt", job.encode(), "text/plain; charset=utf-8")
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": page.uri, "generation": page.generation, "sha256": sha256 or page.sha256},
    }
    application_id, _ = record_capture(
        engine, url=f"https://jobs.ashbyhq.com/fin/{n}", company=company, title=role,
        dedupe_key=f"{n}" * 64, snapshot=refs,
    )  # fmt: skip
    return application_id


def submit(engine: Engine, application_id: int) -> None:
    cv = SubmittedFile("cv", "cv.pdf", "application/pdf", 1234,
                       {"uri": "gs://b/c", "generation": 1, "sha256": "1" * 64})  # fmt: skip
    record_submission(engine, submission_key=f"sub-{application_id}", application_id=application_id,
                      channel="company site", submitted_at="2026-11-02T00:00:00+00:00",
                      files=[cv])  # fmt: skip


def draft(engine: Engine, application_id: int) -> int:
    facts = [FactVersion(1, "role-acme", 1, "role", "ML engineer at Acme; cut cost by 35%",
                         "letter", None, "verified", None)]  # fmt: skip
    body = json.dumps({
        "cv_lines": [{"text": "Cut cost by 35% at Acme", "fact_ids": ["role-acme"]},
                     {"text": "Cut cost by 50% at Acme", "fact_ids": ["role-acme"]}],
        "cover_note": [],
    })  # fmt: skip

    def model(request: ModelRequest) -> ModelResponse:
        record = CallRecord("anthropic", "claude-sonnet-5-5", request.step, 1, 0, 0, 1, 1, None)
        return ModelResponse(text=body, stop="end", record=record)

    with engine.connect() as conn:
        snapshot_id = conn.execute(
            text("SELECT snapshot_id FROM applications WHERE id = :a"), {"a": application_id}
        ).scalar_one()
    checked = draft_pack(model, facts, "A job.", load_claim_levels(REPO_CONFIG))
    return store_draft(engine, checked, application_id=application_id,
                       thread_id=f"pack-{application_id}-1", snapshot_id=snapshot_id)  # fmt: skip


def test_the_list_shows_every_application_with_its_stage_submissions_and_drafts(
    engine: Engine, records: RecordStore, client: TestClient, record_prefix: str
) -> None:
    empty = client.get("/applications")
    assert empty.status_code == 200 and "No applications yet" in empty.text

    first = capture(engine, records, record_prefix, company="Fin <AI>", n=1)
    second = capture(engine, records, record_prefix, company="Acme", role="Platform", n=2)
    submit(engine, first)
    draft(engine, first)
    page = client.get("/applications").text

    assert f'href="/applications/{first}"' in page and f'href="/applications/{second}"' in page
    assert "Fin &lt;AI&gt;" in page and "Fin <AI>" not in page  # board text is escaped
    row = page[page.index(f'href="/applications/{first}"') :]
    row = row[: row.index("</tr>")]
    assert "submitted" in row  # the stage after a submission
    assert "1 submission" in row and "2026-11-02" in row
    assert "draft 1" in row and "1 accepted, 1 blocked" in row


def test_an_application_page_shows_its_stored_job_description(
    engine: Engine, records: RecordStore, client: TestClient, record_prefix: str
) -> None:
    application_id = capture(engine, records, record_prefix)
    page = client.get(f"/applications/{application_id}")
    assert page.status_code == 200
    body = page.text
    assert "Fin" in body and "AI Engineer" in body and "captured" in body
    # Exactly what was captured, escaped, in its own block.
    assert "You&#x27;ll build &lt;agents&gt; &amp; evals." in body
    assert "<agents>" not in body
    assert 'href="https://jobs.ashbyhq.com/fin/1"' in body


def test_a_job_description_that_fails_its_hash_check_isnt_shown(
    engine: Engine, records: RecordStore, client: TestClient, record_prefix: str
) -> None:
    application_id = capture(engine, records, record_prefix, sha256="f" * 64)
    body = client.get(f"/applications/{application_id}").text
    assert "can't show the job description" in body.lower()
    assert "sha256" in body  # the reason
    assert "build &lt;agents&gt;" not in body  # never the unchecked bytes
    assert "Events" in body  # the rest of the page still renders


def test_the_page_lists_events_submissions_and_drafts(
    engine: Engine, records: RecordStore, client: TestClient, record_prefix: str
) -> None:
    application_id = capture(engine, records, record_prefix)
    submit(engine, application_id)
    draft_id = draft(engine, application_id)
    body = client.get(f"/applications/{application_id}").text

    events = body[body.index("Events") : body.index("Submissions")]
    assert "captured" in events and "submission.confirmed" in events
    submissions = body[body.index("Submissions") : body.index("Drafts")]
    assert "company site" in submissions and "2026-11-02" in submissions
    assert "cv v1: cv.pdf" in submissions and "1234 bytes" in submissions
    drafts = body[body.index("Drafts") :]
    assert f"Draft {draft_id}" in drafts and "claude-sonnet-5-5" in drafts
    assert "1 accepted, 1 blocked" in drafts


def test_an_unknown_application_is_a_404(client: TestClient) -> None:
    assert client.get("/applications/999999").status_code == 404


def test_every_page_links_the_digest_and_the_applications(client: TestClient) -> None:
    for path in ("/", "/applications"):
        body = client.get(path).text
        assert 'href="/"' in body and 'href="/applications"' in body


def test_the_digest_links_a_captured_postings_application(
    engine: Engine, records: RecordStore, client: TestClient, record_prefix: str
) -> None:
    from jhunt.workflows.scout.parse import Posting
    from jhunt.workflows.scout.postings import upsert_postings

    application_id = capture(engine, records, record_prefix)
    upsert_postings(engine, [Posting(
        dedupe_key="1" * 64, provider="ashby", board="fin", job_id="1",
        url="https://jobs.ashbyhq.com/fin/1", title="AI Engineer", company="Fin",
        location="Dublin", remote=False, in_scope=True, scope_reason="Dublin",
        description_html="", fit_score=0.6, fit_matches={"title": ["ai engineer"]},
    )])  # fmt: skip
    body = client.get("/").text
    assert f'<a href="/applications/{application_id}">application {application_id}</a>' in body


def test_a_job_description_that_cant_be_read_says_why_and_shows_the_rest(
    engine: Engine, client: TestClient, gcs_test_bucket: str, record_prefix: str
) -> None:
    # The snapshot names an object that isn't there: storage says so.
    missing = f"gs://{gcs_test_bucket}/{record_prefix}missing/page.txt"
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": missing, "generation": 1, "sha256": "0" * 64},
    }
    application_id, _ = record_capture(
        engine, url="https://jobs.ashbyhq.com/fin/9", company="Fin", title="AI Engineer",
        dedupe_key="9" * 64, snapshot=refs,
    )  # fmt: skip
    page = client.get(f"/applications/{application_id}")
    assert page.status_code == 200
    assert "can't show the job description: notfound" in page.text.lower()
    assert "Events" in page.text and "Submissions" in page.text
