"""jhunt-pack queues a new draft for an application each time, and prints a
stored draft: the cover note, the CV lines, then the blocked claims (ADR 0016)."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.app import pack as cmd
from jhunt.platform.memory.claims import load_claim_levels
from jhunt.platform.memory.facts import FactVersion
from jhunt.platform.models import CallRecord, ModelRequest, ModelResponse
from jhunt.workflows import application_pack
from jhunt.workflows.application_pack.pack import draft_pack, store_draft
from jhunt.workflows.capture.record import record_capture

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def engine(
    monkeypatch: pytest.MonkeyPatch, migrated_db_url: URL, gcs_test_bucket: str
) -> Iterator[Engine]:
    # The scratch database and the CI bucket; no model key is needed to queue.
    monkeypatch.setenv("POSTGRES_DB", str(migrated_db_url.database))
    monkeypatch.setenv("GCS_RECORDS_BUCKET", gcs_test_bucket)
    monkeypatch.setenv("GCS_STAGING_BUCKET", gcs_test_bucket)
    monkeypatch.setenv("JHUNT_CONFIG_DIR", str(REPO_CONFIG))
    cmd.get_settings.cache_clear()
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()
    cmd.get_settings.cache_clear()


def capture(engine: Engine) -> tuple[int, int]:
    refs = {
        "html": {"uri": "gs://b/h", "generation": 1, "sha256": "aa"},
        "text": {"uri": "gs://b/t", "generation": 2, "sha256": "bb"},
    }
    application_id, _ = record_capture(
        engine, url="https://x/1", company="Fin", title="AI Engineer", dedupe_key="8" * 64,
        snapshot=refs,
    )  # fmt: skip
    with engine.connect() as conn:
        snapshot_id: int = conn.execute(text("SELECT snapshot_id FROM applications")).scalar_one()
    return application_id, snapshot_id


def jhunt_pack(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
               *args: str) -> str:  # fmt: skip
    monkeypatch.setattr(sys, "argv", ["jhunt-pack", *args])
    cmd.main()
    return capsys.readouterr().out.strip()


def test_each_request_queues_a_new_draft(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    application_id, _ = capture(engine)
    first = application_pack.thread_id_for(application_id, 1)
    second = application_pack.thread_id_for(application_id, 2)
    assert jhunt_pack(monkeypatch, capsys, str(application_id)) == f"queued: thread {first}"
    assert jhunt_pack(monkeypatch, capsys, str(application_id)) == f"queued: thread {second}"
    with engine.connect() as conn:
        payloads = (
            conn.execute(
                text("SELECT payload FROM runs WHERE workflow = 'application_pack' ORDER BY id")
            )
            .scalars()
            .all()
        )
    assert payloads == [{"application_id": application_id}] * 2


def test_an_unknown_application_is_refused(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="jhunt-pack: no application 999"):
        jhunt_pack(monkeypatch, capsys, "999")


def test_a_stored_draft_prints_the_note_the_lines_and_what_was_blocked(engine: Engine) -> None:
    application_id, snapshot_id = capture(engine)
    facts = [
        FactVersion(1, "role-acme", 1, "role", "ML engineer at Acme; cut inference cost by 35%",
                    "letter", None, "verified", None),
    ]  # fmt: skip
    drafted = {
        "cv_lines": [{"text": "Cut inference cost by 35% at Acme", "fact_ids": ["role-acme"]},
                     {"text": "Cut inference cost by 50% at Acme", "fact_ids": ["role-acme"]}],
        "cover_note": [{"text": "At Acme I cut inference cost by 35%.", "fact_ids": ["role-acme"],
                        "group": "experience"}],
    }  # fmt: skip

    def model(request: ModelRequest) -> ModelResponse:
        record = CallRecord("anthropic", "claude-sonnet-5-5", request.step, 1, 0, 0, 1, 1, None)
        return ModelResponse(text=json.dumps(drafted), stop="end", record=record)

    draft = draft_pack(model, facts, "A job.", load_claim_levels(REPO_CONFIG))
    draft_id = store_draft(engine, draft, application_id=application_id, thread_id="pack-x-1",
                           snapshot_id=snapshot_id)  # fmt: skip

    shown = cmd.show_draft(engine, draft_id, cmd.load_pack_config(REPO_CONFIG))
    assert shown.index("Cover note") < shown.index("CV lines") < shown.index("Repair round")
    assert "Dear Fin hiring team" in shown and "At Acme I cut inference cost by 35%." in shown
    assert "- Cut inference cost by 35% at Acme  [role-acme]" in shown
    # The stand-in repeats its draft in the repair round: blocked in both.
    assert "Repair round: 1 blocked claims went back to the model once" in shown
    first = shown[shown.index("Blocked in the first draft") : shown.index("Still blocked")]
    still = shown[shown.index("Still blocked after the repair") :]
    for part in (first, still):
        assert "Cut inference cost by 50% at Acme" in part
        assert "number '50%' is not in its cited facts" in part
    assert "50%" not in shown[: shown.index("Repair round")]  # never in the note or the lines
