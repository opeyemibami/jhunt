"""The pack on the application page (ADR 0017, U4): Draft pack, live; a draft
with accepted and blocked claims side by side, with the reasons; edit a line
(each save a new version, created_by you, with the validator's warnings
shown but never blocking the save); copy the cover note."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

from jhunt.api.digest import create_app
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.memory.claims import load_claim_levels
from jhunt.platform.memory.facts import import_facts, latest_facts, read_facts
from jhunt.platform.models import CallRecord, ModelRequest, ModelResponse
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows import application_pack
from jhunt.workflows.application_pack.pack import draft_pack, store_draft
from jhunt.workflows.capture.record import record_capture

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
POST = {"headers": {"Origin": ORIGIN}, "follow_redirects": False}
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
FACTS = """
[[fact]]
id = "role-acme"
kind = "role"
text = "ML engineer at Acme; cut inference cost by 35%"
evidence = "reference letter"
verified_at = 2026-10-30
status = "verified"
"""
DRAFTED = {
    "cv_lines": [
        {"text": "Cut inference cost by 35% at Acme", "fact_ids": ["role-acme"]},
        {"text": "Cut inference cost by 50% at Acme", "fact_ids": ["role-acme"]},
    ],
    "cover_note": [
        {"text": "At Acme I cut inference cost by 35%.", "fact_ids": ["role-acme"],
         "group": "experience"},
    ],
}  # fmt: skip


@pytest.fixture
def engine(migrated_db_url: URL, tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    (tmp_path / "facts.toml").write_text(FACTS)
    import_facts(eng, read_facts(tmp_path / "facts.toml"))
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    registry = Registry()
    registry.register(application_pack.WORKFLOW, application_pack.VERSION,
                      application_pack.build(ToolGateway(engine, [])))  # fmt: skip
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app = create_app(engine, registry, port=PORT, config_dir=REPO_CONFIG, conninfo=conninfo,
                     records=RecordStore("unused"))  # fmt: skip
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def application(engine: Engine) -> int:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    found, _ = record_capture(engine, url="https://x/1", company="Fin", title="AI Engineer",
                              dedupe_key="4" * 64, snapshot=refs)  # fmt: skip
    return found


def drafted(engine: Engine, application_id: int, thread: str = "pack-x-1") -> int:
    def model(request: ModelRequest) -> ModelResponse:
        record = CallRecord("anthropic", "claude-sonnet-5-5", request.step, 1, 0, 0, 1, 1, None)
        return ModelResponse(text=json.dumps(DRAFTED), stop="end", record=record)

    with engine.connect() as conn:
        snapshot_id = conn.execute(
            text("SELECT snapshot_id FROM applications WHERE id = :a"), {"a": application_id}
        ).scalar_one()
    checked = draft_pack(model, latest_facts(engine), "A job.", load_claim_levels(REPO_CONFIG),
                         company="Fin", role="AI Engineer")  # fmt: skip
    return store_draft(engine, checked, application_id=application_id, thread_id=thread,
                       snapshot_id=snapshot_id)  # fmt: skip


def pack_runs(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(
            "SELECT id, thread_id, payload FROM runs WHERE workflow = 'application_pack'"
            " ORDER BY id"
        )))  # fmt: skip


def test_draft_pack_queues_a_new_draft_and_shows_it_live(
    engine: Engine, client: TestClient
) -> None:
    app_id = application(engine)
    page = client.get(f"/applications/{app_id}").text
    assert f'action="/applications/{app_id}/pack"' in page

    assert client.post(f"/applications/{app_id}/pack", **POST).status_code == 303
    [run] = pack_runs(engine)
    assert (run.thread_id, run.payload) == (f"pack-{app_id}-1", {"application_id": app_id})
    page = client.get(f"/applications/{app_id}").text
    assert re.search(rf'data-run="{run.id}"[^>]*>Drafting: queued<', page)

    client.post(f"/applications/{app_id}/pack", **POST)  # each request is a new draft
    assert [r.thread_id for r in pack_runs(engine)] == [f"pack-{app_id}-1", f"pack-{app_id}-2"]


def test_a_finished_draft_says_so_in_its_live_status(engine: Engine, client: TestClient) -> None:
    app_id = application(engine)
    client.post(f"/applications/{app_id}/pack", **POST)
    [run] = pack_runs(engine)
    draft_id = drafted(engine, app_id, thread=run.thread_id)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'succeeded' WHERE id = :i"), {"i": run.id})
    status = client.get(f"/runs/{run.id}").json()
    # The stand-in repeats its draft in the repair round: the 50% line stays blocked.
    assert status == {"status": "succeeded", "text": f"draft {draft_id}: 2 accepted, 1 blocked"}


def test_a_draft_shows_accepted_and_blocked_side_by_side(
    engine: Engine, client: TestClient
) -> None:
    draft_id = drafted(engine, application(engine))
    page = client.get(f"/drafts/{draft_id}").text
    accepted = page[
        page.index('<section class="accepted"') : page.index('<section class="blocked"')
    ]
    blocked = page[page.index('<section class="blocked"') :]
    assert '<div class="side-by-side">' in page
    # The cover note, framed, and the CV lines with their facts.
    assert "Dear Fin hiring team" in accepted and "At Acme I cut inference cost by 35%." in accepted
    assert "Cut inference cost by 35% at Acme" in accepted and "role-acme" in accepted
    assert "50%" not in accepted
    # Every blocked claim, as drafted, with its reasons.
    assert "Cut inference cost by 50% at Acme" in blocked
    # Each reason on its own line under the blocked text, in plain words.
    assert re.search(
        r"Cut inference cost by 50% at Acme</p>\s*<p class=\"problem\">"
        r"Says &#x27;50%&#x27;, but no cited fact has that number</p>",
        blocked,
    )


def test_the_cover_note_can_be_copied(engine: Engine, client: TestClient) -> None:
    draft_id = drafted(engine, application(engine))
    page = client.get(f"/drafts/{draft_id}").text
    area = re.search(r'<textarea id="cover-note" readonly[^>]*>(.*?)</textarea>', page, re.S)
    assert area is not None and "At Acme I cut inference cost by 35%." in area.group(1)
    assert "navigator.clipboard.writeText" in page and 'id="copy-note"' in page


def edit(client: TestClient, draft_id: int, new: str, *, section: str = "cv_line",
         position: int = 0, round_: int = 1) -> Any:  # fmt: skip
    return client.post(
        f"/drafts/{draft_id}/edit",
        data={"round": str(round_), "section": section, "position": str(position), "text": new},
        **POST,
    )


def test_an_edit_is_a_new_version_and_the_validator_warns_but_never_blocks(
    engine: Engine, client: TestClient
) -> None:
    draft_id = drafted(engine, application(engine))
    assert edit(client, draft_id, "Cut Acme's inference cost by 35%").status_code == 303
    assert edit(client, draft_id, "Cut Acme's inference cost by 40%").status_code == 303  # saved

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT version, text, created_by, warnings FROM draft_edits ORDER BY version"
        )).all()  # fmt: skip
        labels = conn.execute(text("SELECT kind, detail FROM labels ORDER BY id")).all()
    assert [(r.version, r.text, r.created_by) for r in rows] == [
        (1, "Cut Acme's inference cost by 35%", "you"),
        (2, "Cut Acme's inference cost by 40%", "you"),
    ]
    assert rows[0].warnings == [] and rows[1].warnings == ["number '40%' is not in its cited facts"]
    assert [lb.kind for lb in labels] == ["draft_edited", "draft_edited"]
    assert labels[1].detail["before"] == "Cut Acme's inference cost by 35%"

    page = client.get(f"/drafts/{draft_id}").text
    assert "Cut Acme&#x27;s inference cost by 40%" in page and "edited by you · v2" in page
    assert "Warning: Says &#x27;40%&#x27;, but no cited fact has that number" in page


def test_an_edited_cover_note_sentence_is_what_gets_copied(
    engine: Engine, client: TestClient
) -> None:
    draft_id = drafted(engine, application(engine))
    edit(client, draft_id, "At Acme, I cut inference cost by 35%.", section="cover_note")
    page = client.get(f"/drafts/{draft_id}").text
    area = re.search(r'<textarea id="cover-note" readonly[^>]*>(.*?)</textarea>', page, re.S)
    assert area is not None and "At Acme, I cut inference cost by 35%." in area.group(1)


def test_draft_edits_never_change(engine: Engine, client: TestClient) -> None:
    draft_id = drafted(engine, application(engine))
    edit(client, draft_id, "Cut Acme's inference cost by 35%")
    for statement in ("UPDATE draft_edits SET text = 'x'", "DELETE FROM draft_edits"):
        with pytest.raises(IntegrityError, match="refused"), engine.begin() as conn:
            conn.execute(text(statement))


def test_bad_requests_are_refused(engine: Engine, client: TestClient) -> None:
    draft_id = drafted(engine, application(engine))
    assert client.get("/drafts/999999").status_code == 404
    assert edit(client, draft_id, "x", position=99).status_code == 404  # no such line
    # The 50% line is blocked: it stays as drafted, so it can't be edited.
    assert edit(client, draft_id, "Cut inference cost by 35%", position=1).status_code == 404
    assert edit(client, draft_id, "   ").status_code == 400  # empty text
    form = {"round": "1", "section": "cv_line", "position": "0", "text": "x"}
    no_origin = client.post(f"/drafts/{draft_id}/edit", follow_redirects=False, data=form)
    assert no_origin.status_code == 403
    assert client.post("/applications/1/pack", follow_redirects=False).status_code == 403


@pytest.mark.parametrize(
    ("problem", "plain"),
    [
        ("cites no fact", "Cites no fact, so nothing supports it"),
        ("cites unknown fact 'x'", "Cites 'x', which isn't in your fact bank"),
        ("fact 'x' is draft, not verified", "Cites 'x', which you haven't verified (draft)"),
        ("mentions unverified skill 'skill-x' (draft)",
         "Names a skill you haven't verified (skill-x)"),
        ("number '50%' is not in its cited facts", "Says '50%', but no cited fact has that number"),
        ("spelled-out number 'five' is not in its cited facts",
         "Says 'five', but no cited fact has that number"),
        ("states built, above 'skill-sglang' (used): 'built'",
         "Says 'built' but the fact only supports 'used' (skill-sglang)"),
        ("states production, above 'skill-k8s' (used): 'in production' (mentioned, not cited)",
         "Says 'in production' but the fact only supports 'used' (skill-k8s, named but not"
         " cited)"),
        ("something new", "something new"),  # unknown wording: shown as it is
    ],
)  # fmt: skip
def test_reasons_in_plain_words(problem: str, plain: str) -> None:
    from jhunt.api.pack import plain_reason

    assert plain_reason(problem) == plain
