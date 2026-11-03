"""The Facts page (ADR 0017, U6): edit a fact's text, evidence, level and
aliases, or retire it. Each save is a new version, through the same checks as
an import, and is marked as made in the browser; older versions are kept."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.memory.facts import import_facts, read_facts
from jhunt.platform.runtime.registry import Registry

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
POST = {"headers": {"Origin": ORIGIN}, "follow_redirects": False}
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
FACTS = """
[[fact]]
id = "role-acme"
kind = "role"
text = "ML engineer at Acme"
evidence = "reference letter"
verified_at = 2026-10-30
status = "verified"

[[fact]]
id = "skill-k8s"
kind = "skill"
text = "Kubernetes"
evidence = "course"
verified_at = 2026-10-30
status = "verified"
level = "used"
"""


@pytest.fixture
def engine(migrated_db_url: URL, tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    (tmp_path / "facts.toml").write_text(FACTS)
    import_facts(eng, read_facts(tmp_path / "facts.toml"))
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app = create_app(engine, Registry(), port=PORT, config_dir=REPO_CONFIG, conninfo=conninfo,
                     records=RecordStore("unused"))  # fmt: skip
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def versions(engine: Engine, fact_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(
            text("SELECT version, text, evidence, level, aliases, status, verified_at, via"
                 " FROM facts WHERE fact_id = :f ORDER BY version"),
            {"f": fact_id},
        ))  # fmt: skip


def edit(client: TestClient, fact_id: str, version: int = 1, **fields: str) -> Any:
    form = {"version": str(version), "text": "ML engineer at Acme", "evidence": "reference letter",
            "level": "", "aliases": "", **fields}  # fmt: skip
    return client.post(f"/facts/{fact_id}/edit", data=form, **POST)


def test_an_edit_is_a_new_version_made_in_the_browser(engine: Engine, client: TestClient) -> None:
    page = client.get("/facts").text
    assert 'action="/facts/role-acme/edit"' in page and 'action="/facts/skill-k8s/edit"' in page

    saved = edit(client, "role-acme", text="Senior ML engineer at Acme", evidence="payslip")
    assert saved.status_code == 303
    [v1, v2] = versions(engine, "role-acme")
    assert (v1.text, v1.via) == ("ML engineer at Acme", "command")  # kept as it was
    assert (v2.version, v2.text, v2.evidence, v2.via) == (
        2, "Senior ML engineer at Acme", "payslip", "browser",
    )  # fmt: skip
    # What the edit doesn't touch stays: still verified, on the same date.
    assert (v2.status, str(v2.verified_at)) == ("verified", "2026-10-30")

    page = client.get("/facts").text
    assert "Senior ML engineer at Acme" in page and "v2 · edited in the browser" in page


def test_a_skills_level_and_aliases(engine: Engine, client: TestClient) -> None:
    saved = edit(client, "skill-k8s", text="Kubernetes", evidence="course", level="built",
                 aliases="Kubernetes, k8s")  # fmt: skip
    assert saved.status_code == 303
    latest = versions(engine, "skill-k8s")[-1]
    assert (latest.level, latest.aliases) == ("built", ["Kubernetes", "k8s"])


def test_the_same_checks_as_an_import(engine: Engine, client: TestClient) -> None:
    refused = [
        edit(client, "role-acme", text="   "),  # no text
        edit(client, "role-acme", evidence=""),  # a verified fact needs evidence
        edit(client, "role-acme", level="built"),  # levels are for skills
        edit(client, "role-acme", aliases="Acme"),  # so are aliases
        edit(client, "skill-k8s", text="Kubernetes", evidence="course", level=""),  # needs one
        edit(client, "skill-k8s", text="Kubernetes", evidence="course", level="expert"),
    ]
    assert [r.status_code for r in refused] == [400] * 6
    assert "text is required" in refused[0].text
    assert "a verified fact needs evidence" in refused[1].text
    assert "a verified skill needs a level" in refused[4].text
    assert len(versions(engine, "role-acme")) == len(versions(engine, "skill-k8s")) == 1


def test_an_unchanged_save_writes_nothing(engine: Engine, client: TestClient) -> None:
    assert edit(client, "role-acme").status_code == 303
    assert len(versions(engine, "role-acme")) == 1


def test_a_save_from_an_old_page_is_refused(engine: Engine, client: TestClient) -> None:
    edit(client, "role-acme", text="Senior ML engineer at Acme")
    stale = edit(client, "role-acme", version=1, text="Lead ML engineer at Acme")
    assert stale.status_code == 409 and "changed since" in stale.text
    assert [v.text for v in versions(engine, "role-acme")] == [
        "ML engineer at Acme", "Senior ML engineer at Acme",
    ]  # fmt: skip


def retire(client: TestClient, fact_id: str, typed: str, version: int = 1) -> Any:
    form = {"version": str(version), "typed": typed}
    return client.post(f"/facts/{fact_id}/retire", data=form, **POST)


def test_retire_needs_the_id_typed_back(engine: Engine, client: TestClient) -> None:
    assert retire(client, "role-acme", "role").status_code == 400
    assert len(versions(engine, "role-acme")) == 1

    assert retire(client, "role-acme", "role-acme").status_code == 303
    latest = versions(engine, "role-acme")[-1]
    assert (latest.version, latest.status, latest.via) == (2, "retired", "browser")
    # A retired fact has no edit form, and an edit is refused.
    assert 'action="/facts/role-acme/edit"' not in client.get("/facts").text
    assert edit(client, "role-acme", version=2, text="Back again").status_code == 400
    assert retire(client, "role-acme", "role-acme", version=2).status_code == 400


def test_bad_requests_are_refused(client: TestClient) -> None:
    assert edit(client, "nope").status_code == 404
    form = {"version": "1", "text": "x", "evidence": "y", "level": "", "aliases": ""}
    no_origin = client.post("/facts/role-acme/edit", data=form, follow_redirects=False)
    assert no_origin.status_code == 403
    no_origin = client.post("/facts/role-acme/retire", data={"version": "1", "typed": "role-acme"},
                            follow_redirects=False)  # fmt: skip
    assert no_origin.status_code == 403


def test_values_are_escaped(engine: Engine, client: TestClient) -> None:
    edit(client, "role-acme", text="ML <b>engineer</b>")
    page = client.get("/facts").text
    assert "ML &lt;b&gt;engineer&lt;/b&gt;" in page and "<b>engineer" not in page
