"""Hide and Archive (owner, UI v1): "Not interested" hides a posting for good,
even when Scout sees it again, as a skip label (design.md's Label entity,
append-only); the Hidden view lists them with Undo. Archive sets an
application's outcome (withdrawn or not pursuing) and moves it out of the
main list; Restore brings it back."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows.capture.record import record_capture
from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
POST = {"headers": {"Origin": ORIGIN}, "follow_redirects": False}
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app = create_app(engine, Registry(), port=PORT, config_dir=REPO_CONFIG, conninfo=conninfo,
                     records=RecordStore("unused"))  # fmt: skip
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def posting(job_id: str, title: str) -> Posting:
    return Posting(
        dedupe_key=job_id * 64, provider="greenhouse", board="acme", job_id=job_id,
        url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}", title=title, company="Acme",
        location="Dublin", remote=False, in_scope=True, scope_reason="Dublin",
        description_html="", fit_score=0.7, fit_matches={"title": ["ml engineer"]},
    )  # fmt: skip


def posting_id(engine: Engine, job_id: str) -> int:
    with engine.connect() as conn:
        found: int = conn.execute(
            text("SELECT id FROM postings WHERE job_id = :j"), {"j": job_id}
        ).scalar_one()
    return found


def labels(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text("SELECT kind, target FROM labels ORDER BY id"))]


def test_not_interested_hides_a_posting_and_records_a_skip(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, [posting("1", "ML Engineer"), posting("2", "Data Engineer")])
    assert "Not interested" in client.get("/").text
    response = client.post(f"/postings/{posting_id(engine, '1')}/hide", **POST)
    assert response.status_code == 303
    digest = client.get("/").text
    assert "ML Engineer" not in digest and "Data Engineer" in digest
    assert 'href="/hidden"' in digest and "1 hidden by you" in digest
    assert labels(engine) == [("skip", "1" * 64)]


def test_a_hidden_posting_stays_hidden_when_scout_sees_it_again(
    engine: Engine, client: TestClient
) -> None:
    upsert_postings(engine, [posting("1", "ML Engineer")])
    client.post(f"/postings/{posting_id(engine, '1')}/hide", **POST)
    upsert_postings(engine, [posting("1", "ML Engineer (re-posted)")])  # tomorrow's Scout
    assert "ML Engineer" not in client.get("/").text


def test_the_hidden_view_lists_them_with_undo(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting("1", "ML Engineer <x>")])
    client.post(f"/postings/{posting_id(engine, '1')}/hide", **POST)
    hidden = client.get("/hidden").text
    assert "ML Engineer &lt;x&gt;" in hidden and "Acme" in hidden
    assert f'action="/hidden/{"1" * 64}/undo"' in hidden

    client.post(f"/hidden/{'1' * 64}/undo", **POST)
    assert "ML Engineer &lt;x&gt;" in client.get("/").text
    assert "ML Engineer" not in client.get("/hidden").text
    assert labels(engine) == [("skip", "1" * 64), ("unskip", "1" * 64)]  # nothing deleted


def test_a_posting_gone_from_postings_stays_in_the_hidden_view(
    engine: Engine, client: TestClient
) -> None:
    # The 30-day cleanup deleted the row; the label remembers what it was.
    upsert_postings(engine, [posting("1", "ML Engineer")])
    client.post(f"/postings/{posting_id(engine, '1')}/hide", **POST)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM postings"))
    assert "ML Engineer" in client.get("/hidden").text


def test_labels_never_change(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting("1", "ML Engineer")])
    client.post(f"/postings/{posting_id(engine, '1')}/hide", **POST)
    for statement in ("UPDATE labels SET kind = 'unskip'", "DELETE FROM labels", "TRUNCATE labels"):
        with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
            conn.execute(text(statement))


def application(engine: Engine, n: int, role: str) -> int:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    found, _ = record_capture(engine, url=f"https://x/{n}", company="Fin", title=role,
                              dedupe_key=f"{n}" * 64, snapshot=refs)  # fmt: skip
    return found


@pytest.mark.parametrize(("outcome", "shown"), [("withdrawn", "withdrawn"),
                                               ("not_pursuing", "not pursuing")])  # fmt: skip
def test_archive_sets_the_outcome_and_moves_it_out_of_the_main_list(
    engine: Engine, client: TestClient, outcome: str, shown: str
) -> None:
    kept, archived = application(engine, 1, "Kept Role"), application(engine, 2, "Gone Role")
    response = client.post(f"/applications/{archived}/archive/{outcome}", **POST)
    assert response.status_code == 303
    main = client.get("/applications").text
    assert "Kept Role" in main and "Gone Role" not in main
    assert 'href="/applications/archived"' in main and "1 archived" in main
    archive = client.get("/applications/archived").text
    assert "Gone Role" in archive and shown in archive and "Kept Role" not in archive
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT outcome, closed_at IS NOT NULL FROM applications WHERE id = :a"),
            {"a": archived},
        ).one()
        event = conn.execute(
            text("SELECT payload FROM events WHERE application_id = :a AND type = 'archived'"),
            {"a": archived},
        ).scalar_one()
    assert tuple(row) == (outcome, True) and event == {"outcome": outcome}
    assert kept != archived


def test_restore_brings_an_archived_application_back(engine: Engine, client: TestClient) -> None:
    archived = application(engine, 2, "Gone Role")
    client.post(f"/applications/{archived}/archive/withdrawn", **POST)
    page = client.get(f"/applications/{archived}").text
    assert "Outcome: withdrawn" in page and f'action="/applications/{archived}/restore"' in page
    client.post(f"/applications/{archived}/restore", **POST)
    assert "Gone Role" in client.get("/applications").text
    with engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM applications WHERE id = :a"), {"a": archived}
        ).scalar_one()
        kinds = (
            conn.execute(
                text("SELECT type FROM events WHERE application_id = :a ORDER BY id"),
                {"a": archived},
            )
            .scalars()
            .all()
        )
    assert outcome is None and kinds == ["captured", "archived", "restored"]


@pytest.mark.parametrize(
    "path",
    ["/postings/1/hide", f"/hidden/{'1' * 64}/undo", "/applications/1/archive/withdrawn",
     "/applications/1/restore"],
)  # fmt: skip
def test_hide_and_archive_need_this_pages_origin(client: TestClient, path: str) -> None:
    assert client.post(path, follow_redirects=False).status_code == 403


def test_an_unknown_outcome_is_refused(engine: Engine, client: TestClient) -> None:
    archived = application(engine, 2, "Gone Role")
    assert client.post(f"/applications/{archived}/archive/hired", **POST).status_code == 404
