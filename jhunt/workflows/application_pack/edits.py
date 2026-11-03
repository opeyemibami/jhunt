"""Queueing a draft, and the owner's edits to drafted lines (ADR 0017, U4).

start_draft is shared by jhunt-pack and the application page: each request is
a new thread, pack-<application_id>-<n>, so earlier drafts are kept.

save_edit stores an edit as a new version of the line (draft_edits), never
changing the drafted claim, and records a draft_edited label (design.md's
eval data: what the owner changed). The claim validator checks the edited
text against the line's cited facts; its problems are kept as warnings and
never block the save (owner, UI v1). Only accepted lines of the round a draft
uses can be edited: a blocked claim stays as drafted.
"""

import json
from dataclasses import dataclass

from sqlalchemy import Engine, text

from jhunt.platform.memory.claims import Claim, ClaimLevels, check_claims
from jhunt.platform.memory.facts import latest_facts
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import enqueue_start
from jhunt.workflows.application_pack.graph import WORKFLOW, thread_id_for


@dataclass(frozen=True)
class Edit:
    version: int
    text: str
    warnings: tuple[str, ...]


def start_draft(engine: Engine, registry: Registry, application_id: int) -> str:
    """Queue a new draft for the application. LookupError if there's none;
    ThreadExists if another request took the number just now."""
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM applications WHERE id = :id"), {"id": application_id}
        ).first()
        if found is None:
            raise LookupError(f"no application {application_id}")
        earlier = conn.execute(
            text("SELECT count(*) FROM threads WHERE thread_id LIKE :p"),
            {"p": f"pack-{application_id}-%"},
        ).scalar_one()
    thread_id = thread_id_for(application_id, earlier + 1)
    enqueue_start(engine, registry, thread_id, WORKFLOW, {"application_id": application_id})
    return thread_id


def save_edit(
    engine: Engine,
    levels: ClaimLevels,
    *,
    draft_id: int,
    round_: int,
    section: str,
    position: int,
    new_text: str,
) -> Edit:
    """A new version of one accepted line. LookupError: no such accepted line
    in the round the draft uses. ValueError: empty text."""
    new_text = new_text.strip()
    if not new_text:
        raise ValueError("the edited line is empty")
    with engine.begin() as conn:
        claim = conn.execute(
            text(
                "SELECT c.text, c.fact_ids FROM draft_claims c WHERE c.draft_id = :d"
                " AND c.round = :r AND c.section = :s AND c.position = :p AND NOT c.blocked"
                " AND c.round = (SELECT max(round) FROM draft_claims WHERE draft_id = :d)"
                " FOR UPDATE"
            ),
            {"d": draft_id, "r": round_, "s": section, "p": position},
        ).first()
        if claim is None:
            raise LookupError("no such accepted line in this draft")
        before = conn.execute(
            text(
                "SELECT version, text FROM draft_edits WHERE draft_id = :d AND round = :r"
                " AND section = :s AND position = :p ORDER BY version DESC LIMIT 1"
            ),
            {"d": draft_id, "r": round_, "s": section, "p": position},
        ).first()
        version = 1 if before is None else before.version + 1
        [checked] = check_claims(
            [Claim(new_text, tuple(claim.fact_ids))], latest_facts(engine), levels
        ).claims
        conn.execute(
            text(
                "INSERT INTO draft_edits (draft_id, round, section, position, version, text,"
                " warnings, created_by) VALUES (:d, :r, :s, :p, :v, :t, :w, 'you')"
            ),
            {"d": draft_id, "r": round_, "s": section, "p": position, "v": version,
             "t": new_text, "w": list(checked.problems)},
        )  # fmt: skip
        conn.execute(
            text(
                "INSERT INTO labels (kind, target, detail, created_by)"
                " VALUES ('draft_edited', :t, CAST(:d AS jsonb), 'you')"
            ),
            {
                "t": f"draft:{draft_id}:{round_}:{section}:{position}",
                "d": json.dumps(
                    {
                        "version": version,
                        "before": before.text if before else claim.text,
                        "after": new_text,
                    }
                ),
            },
        )
    return Edit(version, new_text, checked.problems)


def latest_edits(engine: Engine, draft_id: int) -> dict[tuple[int, str, int], Edit]:
    """Each edited line's latest version, by (round, section, position)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT ON (round, section, position) round, section, position,"
                " version, text, warnings FROM draft_edits WHERE draft_id = :d"
                " ORDER BY round, section, position, version DESC"
            ),
            {"d": draft_id},
        )
        return {(r.round, r.section, r.position): Edit(r.version, r.text, tuple(r.warnings))
                for r in rows}  # fmt: skip
