"""Recording a submission from the application page (ADR 0018): every file is
checked before anything is staged; staged copies are keyed by content; the
preview's sha256 is the one recorded, and a staged copy changed after the
preview is refused with nothing recorded; the staged copy is deleted once the
submission is recorded. Real buckets: the CI bucket for records, the CI
staging bucket for uploads."""

import hashlib
import html
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.cloud import storage
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import ObjectMissing, RecordStore, StagingStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime import queue
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.worker import process_run
from jhunt.workflows import workspace
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.workspace import submission
from jhunt.workflows.workspace.submission import SubmissionFileChanged, staged_key
from tests.runtime.lease_watch import LeaseWatch

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
POST = {"headers": {"Origin": ORIGIN}, "follow_redirects": False}
KEY = "d" * 64
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def db(migrated_db_url: URL) -> Iterator[tuple[Engine, str]]:
    engine = create_engine(migrated_db_url, poolclass=pool.NullPool)
    info = migrated_db_url.set(drivername="postgresql").render_as_string(hide_password=False)
    yield engine, info
    engine.dispose()


@pytest.fixture
def engine(db: tuple[Engine, str]) -> Engine:
    return db[0]


@pytest.fixture
def staging(gcs_test_staging_bucket: str, gcs_client: storage.Client) -> StagingStore:
    return StagingStore(gcs_test_staging_bucket, gcs_client)


@pytest.fixture
def registry(
    engine: Engine, staging: StagingStore, gcs_test_bucket: str, gcs_client: storage.Client,
    record_prefix: str,
) -> Registry:  # fmt: skip
    store = RecordStore(gcs_test_bucket, gcs_client)
    gateway = ToolGateway(engine, [
        workspace.store_submission_file_tool(store, staging=staging, prefix=record_prefix),
        workspace.delete_staged_submission_file_tool(staging),
    ])  # fmt: skip
    built = Registry()
    built.register(workspace.WORKFLOW, workspace.VERSION, workspace.build(engine, gateway, staging))
    return built


@pytest.fixture
def client(
    db: tuple[Engine, str], registry: Registry, staging: StagingStore
) -> Iterator[TestClient]:
    engine, info = db
    app = create_app(engine, registry, port=PORT, config_dir=REPO_CONFIG, conninfo=info,
                     records=RecordStore("unused"), staging=staging)  # fmt: skip
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


@pytest.fixture
def application(engine: Engine) -> int:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    found, _ = record_capture(engine, url="https://x/1", company="Acme", title="ML Engineer",
                              dedupe_key=KEY, snapshot=refs)  # fmt: skip
    return found


@pytest.fixture
def cv() -> bytes:
    """Bytes no other test uses: staged copies are keyed by content."""
    return f"%PDF the CV I sent {uuid.uuid4()}".encode()


def preview(client: TestClient, application: int, files: list[tuple[str, tuple[str, bytes]]],
            *, channel: str = "company site", day: str = "2026-11-01") -> Any:  # fmt: skip
    return client.post(
        f"/applications/{application}/submission",
        data={"channel": channel, "submitted_at": day},
        files=[(field, (name, data, "application/octet-stream")) for field, (name, data) in files],
        **POST,
    )


def form(page: str) -> dict[str, str]:
    """The preview's confirm form, as the browser would send it."""
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page))
    return {k: html.unescape(v) for k, v in fields.items()}


def confirm(client: TestClient, application: int, fields: dict[str, str], typed: str) -> Any:
    return client.post(f"/applications/{application}/submission/confirm",
                       data={**fields, "typed": typed}, **POST)  # fmt: skip


def work(db: tuple[Engine, str], registry: Registry, lease_watch: LeaseWatch) -> queue.Run:
    engine, info = db
    run = queue.claim(engine)
    assert run is not None, "nothing to claim"
    process_run(engine, info, registry, run, on_lease_lost=lease_watch)
    return run


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def records(gcs_bucket: storage.Bucket, prefix: str) -> list[str]:
    return sorted(b.name for b in gcs_bucket.client.list_blobs(gcs_bucket, prefix=prefix))


def staged(staging: StagingStore, key: str) -> bytes | None:
    found = staging.existing(key)
    return None if found is None else found[1]


def test_the_application_page_has_the_form(client: TestClient, application: int) -> None:
    page = client.get(f"/applications/{application}").text
    assert f'action="/applications/{application}/submission"' in page
    assert 'enctype="multipart/form-data"' in page
    for field in ("cv", "cover_note", "answers", "other", "channel", "submitted_at"):
        assert f'name="{field}"' in page


def test_every_file_is_checked_before_anything_is_staged(
    engine: Engine, client: TestClient, application: int, staging: StagingStore, cv: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(submission, "MAX_FILE_BYTES", 40)
    big = cv + b"x" * 40
    refused = preview(client, application, [
        ("cv", ("cv.pdf", cv)),  # fine on its own: still not staged
        ("cover_note", ("note.exe", b"MZ")),
        ("answers", ("answers.pdf", b"%PDF")),
        ("other", ("big.pdf", big)),
        ("other", ("../etc/x.pdf", b"%PDF")),
    ], day="2999-01-01")  # fmt: skip
    assert refused.status_code == 400
    for problem in (
        "note.exe: not a PDF, DOCX, ODT, TXT or MD file",
        "answers.pdf: answers must be a TXT or MD file",
        f"big.pdf: {len(big)} bytes, over the 40-byte limit",
        "rename it",
        "2999-01-01 is in the future",
    ):
        assert html.escape(problem) in refused.text, problem
    assert count(engine, "tool_calls") == 0  # every staging write goes through the gateway
    for name, data in (("cv.pdf", cv), ("big.pdf", big)):
        assert staged(staging, staged_key(hashlib.sha256(data).hexdigest(), name)) is None

    assert preview(client, application, []).status_code == 400  # nothing given
    assert preview(client, application, [("cv", ("cv.pdf", cv))], channel=" ").status_code == 400
    assert count(engine, "tool_calls") == 0


def test_the_preview_stages_each_file_by_content_and_shows_its_sha256(
    engine: Engine, client: TestClient, application: int, staging: StagingStore, cv: bytes,
) -> None:  # fmt: skip
    answers = b"Why us?\nBecause.\n"
    shown = preview(client, application, [("cv", ("cv.pdf", cv)),
                                          ("answers", ("answers.md", answers))])  # fmt: skip
    assert shown.status_code == 200
    sha = hashlib.sha256(cv).hexdigest()
    assert f"<code>{sha}</code>" in shown.text and "cv.pdf" in shown.text
    assert "Acme, ML Engineer" in shown.text and "company site" in shown.text
    assert staged(staging, f"uploads/{sha}/cv.pdf") == cv  # keyed by content
    assert count(engine, "submissions") == 0  # nothing recorded until confirmed


def test_confirm_records_what_the_preview_showed_and_deletes_the_staged_copy(
    db: tuple[Engine, str], engine: Engine, client: TestClient, registry: Registry,
    application: int, staging: StagingStore, cv: bytes, gcs_bucket: storage.Bucket,
    record_prefix: str, lease_watch: LeaseWatch,
) -> None:  # fmt: skip
    answers = f"Why us? {uuid.uuid4()}".encode()
    shown = preview(client, application, [("cv", ("cv.pdf", cv)),
                                          ("answers", ("answers.md", answers))])  # fmt: skip
    fields = form(shown.text)
    assert confirm(client, application, fields, typed="nope").status_code == 400
    assert count(engine, "runs") == 0

    assert confirm(client, application, fields, typed=str(application)).status_code == 303
    assert confirm(client, application, fields, typed=str(application)).status_code == 303  # twice
    shas = [hashlib.sha256(cv).hexdigest(), hashlib.sha256(answers).hexdigest()]
    thread = submission.submission_thread_id(application, shas, "company site",
                                             "2026-11-01T00:00:00+00:00")  # fmt: skip
    with engine.connect() as conn:
        runs = conn.execute(text("SELECT id, thread_id, status FROM runs")).all()
    assert [(r.thread_id, r.status) for r in runs] == [(thread, "queued")]  # the command's thread
    page = client.get(f"/applications/{application}").text
    assert re.search(rf'data-run="{runs[0].id}"[^>]*>Recording: queued<', page)

    run = work(db, registry, lease_watch)
    assert client.get(f"/runs/{run.id}").json() == {
        "status": "succeeded", "text": "recorded: submission 1 (2 documents)",
    }  # fmt: skip
    with engine.connect() as conn:
        documents = conn.execute(text("SELECT sha256, uri FROM documents ORDER BY kind")).all()
        stage = conn.execute(text("SELECT stage FROM applications")).scalar_one()
    assert sorted(d.sha256 for d in documents) == sorted(shas)  # what the preview showed
    assert stage == "submitted"
    assert len(records(gcs_bucket, f"{record_prefix}{KEY}/")) == 2
    for name, sha in (("cv.pdf", shas[0]), ("answers.md", shas[1])):
        assert staged(staging, staged_key(sha, name)) is None  # cleaned up

    again = preview(client, application, [("cv", ("cv.pdf", cv)),
                                          ("answers", ("answers.md", answers))])  # fmt: skip
    assert "Already recorded" in again.text and "submission 1" in again.text


def replace_staged(staging: StagingStore, key: str, data: bytes) -> int:
    """Change a staged copy after the preview: the same key, other bytes."""
    found = staging.existing(key)
    assert found is not None
    assert staging.delete(key, found[0].generation)
    return staging.put(key, data, "application/pdf").generation


def assert_nothing_recorded(engine: Engine, gcs_bucket: storage.Bucket, prefix: str) -> None:
    assert count(engine, "submissions") == count(engine, "documents") == 0
    assert records(gcs_bucket, prefix) == []


def test_a_staged_copy_changed_after_the_preview_is_refused(
    db: tuple[Engine, str], engine: Engine, client: TestClient, registry: Registry,
    application: int, staging: StagingStore, cv: bytes, gcs_bucket: storage.Bucket,
    record_prefix: str, lease_watch: LeaseWatch,
) -> None:  # fmt: skip
    fields = form(preview(client, application, [("cv", ("cv.pdf", cv))]).text)
    key = staged_key(hashlib.sha256(cv).hexdigest(), "cv.pdf")
    replace_staged(staging, key, b"%PDF some other CV")

    confirm(client, application, fields, typed=str(application))
    run = work(db, registry, lease_watch)
    status = client.get(f"/runs/{run.id}").json()
    assert status["status"] == "failed" and "ObjectMissing" in status["text"]
    assert_nothing_recorded(engine, gcs_bucket, record_prefix)
    page = client.get(f"/applications/{application}").text
    assert "Recording: failed: ObjectMissing" in page


def test_the_sha256_read_is_checked_even_at_the_new_generation(
    db: tuple[Engine, str], engine: Engine, client: TestClient, registry: Registry,
    application: int, staging: StagingStore, cv: bytes, gcs_bucket: storage.Bucket,
    record_prefix: str, lease_watch: LeaseWatch,
) -> None:  # fmt: skip
    """The form names the replaced copy's generation: what's read there is
    hashed, and it isn't the sha256 shown, so nothing is recorded."""
    fields = form(preview(client, application, [("cv", ("cv.pdf", cv))]).text)
    key = staged_key(hashlib.sha256(cv).hexdigest(), "cv.pdf")
    generation = replace_staged(staging, key, b"%PDF some other CV")
    fields["files"] = re.sub(r'"generation": \d+', f'"generation": {generation}', fields["files"])

    confirm(client, application, fields, typed=str(application))
    run = work(db, registry, lease_watch)
    status = client.get(f"/runs/{run.id}").json()
    assert status["status"] == "failed" and "changed after the preview" in status["text"]
    assert_nothing_recorded(engine, gcs_bucket, record_prefix)


def test_the_store_tool_hashes_what_it_read(
    engine: Engine, staging: StagingStore, cv: bytes, gcs_test_bucket: str,
    gcs_client: storage.Client, gcs_bucket: storage.Bucket, record_prefix: str,
) -> None:  # fmt: skip
    """The tool itself refuses a mismatch, not only the check step before it."""
    sha = hashlib.sha256(cv).hexdigest()
    other = staging.put(staged_key(sha, "cv.pdf"), b"%PDF not the CV", "application/pdf")
    tool = workspace.store_submission_file_tool(
        RecordStore(gcs_test_bucket, gcs_client), staging=staging, prefix=record_prefix
    )
    args = {"staged": {"key": other.key, "generation": other.generation}, "dedupe_key": KEY,
            "file_name": "cv.pdf", "content_type": "application/pdf", "sha256": sha}  # fmt: skip
    with pytest.raises(SubmissionFileChanged, match="changed after it was confirmed"):
        tool.run(args)
    assert records(gcs_bucket, record_prefix) == []
    staging.delete(other.key, other.generation)


def test_staging_delete_and_read_are_exact(staging: StagingStore, cv: bytes) -> None:
    key = f"uploads/{hashlib.sha256(cv).hexdigest()}/cv.pdf"
    put = staging.put(key, cv, "application/pdf")
    assert staging.read(key, put.generation) == cv
    assert staging.delete(key, put.generation + 1) is False  # another generation: untouched
    assert staging.read(key, put.generation) == cv
    assert staging.delete(key, put.generation) is True
    assert staging.delete(key, put.generation) is False  # again: already gone
    with pytest.raises(ObjectMissing):
        staging.read(key, put.generation)


def test_bad_confirms_are_refused(
    engine: Engine, client: TestClient, application: int, cv: bytes
) -> None:
    fields = form(preview(client, application, [("cv", ("cv.pdf", cv))]).text)
    tampered = {**fields, "files": fields["files"].replace("uploads/", "elsewhere/")}
    assert confirm(client, application, tampered, typed=str(application)).status_code == 400
    assert confirm(client, application, {**fields, "files": "[]"},
                   typed=str(application)).status_code == 400  # fmt: skip
    typed = {**fields, "typed": str(application)}
    no_origin = client.post(f"/applications/{application}/submission/confirm", data=typed,
                            follow_redirects=False)  # fmt: skip
    assert no_origin.status_code == 403
    no_origin = client.post(f"/applications/{application}/submission",
                            data={"channel": "x", "submitted_at": "2026-11-01"},
                            follow_redirects=False)  # fmt: skip
    assert no_origin.status_code == 403
    assert confirm(client, 999999, fields, typed="999999").status_code == 404
    assert count(engine, "runs") == 0
