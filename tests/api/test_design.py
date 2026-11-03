"""The design pass (owner): one layout for every page, with top navigation
(Digest, Applications, Facts), light and dark, and job cards: the title
prominent, company and location secondary, the score as a small badge,
matched words as small tags, actions on the right, and "New" and "No longer
listed" badges."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.memory.facts import import_facts, read_facts
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
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


def posting(job_id: str, title: str, *, location: str = "Dublin, Ireland") -> Posting:
    return Posting(
        dedupe_key=job_id * 64, provider="greenhouse", board="acme", job_id=job_id,
        url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}", title=title, company="Acme",
        location=location, remote=False, in_scope=True, scope_reason="Dublin",
        description_html="", fit_score=0.82,
        fit_matches={"title": ["ml engineer"], "include": ["llm", "python"],
                     "penalty": ["staff ml"]},
    )  # fmt: skip


def scout_run(engine: Engine, created_at: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (thread_id, workflow, version, kind, payload, status, created_at)"
                " VALUES (:t, 'scout', 2, 'start', '{}', 'succeeded', :c)"
            ),
            {"t": f"scout-{created_at:%Y-%m-%d}", "c": created_at},
        )


def seen(engine: Engine, job_id: str, first: datetime, last: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE postings SET first_seen_at = :f, last_seen_at = :l WHERE job_id = :j"),
            {"f": first, "l": last, "j": job_id},
        )


def card(html: str, job_id: str) -> str:
    """The whole card: from its <li> to its </li>."""
    start = html.rindex("<li", 0, html.index(f'id="posting-{job_id}"'))
    return html[start : html.index("</li>", start)]


def test_every_page_has_the_same_top_navigation(engine: Engine, client: TestClient) -> None:
    for path in ("/", "/applications", "/facts"):
        page = client.get(path)
        assert page.status_code == 200, path
        nav = page.text[page.text.index("<nav") : page.text.index("</nav>")]
        assert [label for _, label in re.findall(r'href="([^"]+)"[^>]*>([^<]+)<', nav)] == [
            "Digest", "Applications", "Facts",
        ]  # fmt: skip


def test_the_current_page_is_marked_in_the_navigation(client: TestClient) -> None:
    nav = client.get("/applications").text
    assert '<a href="/applications" aria-current="page">Applications</a>' in nav


def test_light_and_dark_and_a_phone_width(client: TestClient) -> None:
    html = client.get("/").text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "prefers-color-scheme: dark" in html
    assert "@media (max-width:" in html  # the phone layout
    assert "<link" not in html and "cdn" not in html.lower()  # nothing from elsewhere


def test_a_job_card(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting("1", "ML Engineer <Platform>")])
    one = card(client.get("/").text, "1")
    assert 'class="card"' in one
    # Title prominent and escaped; company and location secondary.
    assert re.search(
        r'<h3 class="card-title"><a class="title" href="[^"]+"[^>]*>'
        r"ML Engineer &lt;Platform&gt;</a>",
        one,
    )
    assert '<p class="card-meta">Acme · Dublin, Ireland</p>' in one
    # The score as a small badge; matched words as small tags, by kind.
    assert '<span class="score" title="Fit score">0.82</span>' in one
    assert '<span class="tag tag-title">ml engineer</span>' in one
    assert (
        '<span class="tag tag-include">llm</span><span class="tag tag-include">python</span>' in one
    )
    assert '<span class="tag tag-penalty">staff ml</span>' in one
    # Actions on the right.
    assert '<div class="card-actions">' in one and "Capture" in one


def test_new_and_no_longer_listed_badges(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting(j, f"Job {j}") for j in ("1", "2", "3")])
    run_at = datetime.now(UTC) - timedelta(hours=1)
    scout_run(engine, run_at)
    seen(engine, "1", run_at + timedelta(minutes=1), run_at + timedelta(minutes=1))  # new
    seen(engine, "2", run_at - timedelta(days=3), run_at + timedelta(minutes=1))  # seen again
    seen(engine, "3", run_at - timedelta(days=3), run_at - timedelta(days=1))  # not seen today
    html = client.get("/").text

    assert '<span class="badge badge-new">New</span>' in card(html, "1")
    assert "badge" not in card(html, "2")
    assert '<span class="badge badge-gone">No longer listed</span>' in card(html, "3")
    assert "badge-new" not in card(html, "3")


def test_no_badges_before_scout_has_run(engine: Engine, client: TestClient) -> None:
    upsert_postings(engine, [posting("1", "Job 1")])
    assert "badge" not in card(client.get("/").text, "1")


def test_the_facts_page_lists_each_facts_latest_version(
    engine: Engine, client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "facts.toml").write_text(
        '[[fact]]\nid = "skill-k8s"\nkind = "skill"\ntext = "Kubernetes <ops>"\n'
        'evidence = "course"\nverified_at = 2026-10-30\nstatus = "verified"\nlevel = "used"\n'
    )
    import_facts(engine, read_facts(tmp_path / "facts.toml"))
    page = client.get("/facts").text
    assert "skill-k8s" in page and "Kubernetes &lt;ops&gt;" in page and "used" in page
    assert "verified" in page and "v1" in page
    assert "1 citable" in page
