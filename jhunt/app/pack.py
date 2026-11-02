"""`jhunt-pack <application_id> [--wait]`: draft an application pack (ADR 0016).

It queues a new draft for the application: CV lines and a cover note, drafted
from your verified facts, every claim checked. A running jhunt-worker does it
(it needs ANTHROPIC_API_KEY). Each request is a new draft; earlier ones are
kept. With --wait it waits and prints the draft: the cover note, the CV lines,
then every blocked claim exactly as drafted, with why it was blocked.

Nothing is sent anywhere: a draft is a suggestion for you to read and use.
"""

import argparse
import time

from sqlalchemy import Engine, create_engine, text

from jhunt.app.registry import build_registry
from jhunt.platform.config import get_settings
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import application_pack
from jhunt.workflows.application_pack import PackConfig, cover_note_text, load_pack_config

WAIT_SECONDS = 300
SECTION_NAMES = {"cover_note": "cover note", "cv_line": "CV line"}


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-pack", description=__doc__.split("\n")[0])
    parser.add_argument("application_id", type=int)
    parser.add_argument("--wait", action="store_true", help="wait for the draft and print it")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url())
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM applications WHERE id = :id"), {"id": args.application_id}
        ).first()
        if found is None:
            raise SystemExit(f"jhunt-pack: no application {args.application_id}")
        # A new thread for each request: pack-<id>-<n>, n one more than the last.
        earlier = conn.execute(
            text("SELECT count(*) FROM threads WHERE thread_id LIKE :p"),
            {"p": f"pack-{args.application_id}-%"},
        ).scalar_one()
    thread_id = application_pack.thread_id_for(args.application_id, earlier + 1)
    try:
        enqueue_start(
            engine,
            build_registry(settings, engine),
            thread_id,
            application_pack.WORKFLOW,
            {"application_id": args.application_id},
        )
    except ThreadExists:  # another jhunt-pack took that number just now
        raise SystemExit(f"jhunt-pack: {thread_id} was just queued by another request") from None
    print(f"queued: thread {thread_id}")
    if args.wait:
        draft_id = _wait(engine, thread_id)
        print(show_draft(engine, draft_id, load_pack_config(settings.jhunt_config_dir)))


def _wait(engine: Engine, thread_id: str) -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            run = conn.execute(
                text("SELECT status, error FROM runs WHERE thread_id = :t"), {"t": thread_id}
            ).one()
            if run.status == "failed":
                raise SystemExit(f"jhunt-pack: the draft failed: {run.error}")
            draft_id: int | None = conn.execute(
                text("SELECT id FROM pack_drafts WHERE thread_id = :t"), {"t": thread_id}
            ).scalar_one_or_none()
        if run.status == "succeeded" and draft_id is not None:
            return draft_id
        time.sleep(1)
    raise SystemExit("jhunt-pack: still drafting; is jhunt-worker running?")


def show_draft(engine: Engine, draft_id: int, config: PackConfig) -> str:
    """The cover note, the CV lines, then every blocked claim as drafted."""
    with engine.connect() as conn:
        head = conn.execute(
            text(
                "SELECT a.company, a.role, d.model FROM pack_drafts d"
                " JOIN applications a ON a.id = d.application_id WHERE d.id = :d"
            ),
            {"d": draft_id},
        ).one()
        claims = conn.execute(
            text(
                "SELECT section, text, fact_ids, blocked, problems FROM draft_claims"
                " WHERE draft_id = :d ORDER BY section DESC, position"
            ),
            {"d": draft_id},
        ).all()
    accepted = [c for c in claims if not c.blocked]
    blocked = [c for c in claims if c.blocked]
    note = cover_note_text(
        [c.text for c in accepted if c.section == "cover_note"],
        config,
        company=head.company,
        role=head.role,
    )
    lines = [f"Draft {draft_id} for {head.company}, {head.role} ({head.model})", "", "Cover note"]
    lines += ["", note, "", "CV lines", ""]
    lines += [f"- {c.text}  [{', '.join(c.fact_ids)}]" for c in accepted if c.section == "cv_line"]
    lines += ["", f"Blocked ({len(blocked)}): not in the note or the lines; never reworded", ""]
    for c in blocked:
        lines.append(f"- {SECTION_NAMES[c.section]}: {c.text}")
        lines += [f"    {problem}" for problem in c.problems]
    return "\n".join(lines)
