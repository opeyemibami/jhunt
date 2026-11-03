"""The pack on the application page (ADR 0017, U4).

POST /applications/<id>/pack  queues a new draft (live on the application page).
GET  /drafts/<id>             a draft: accepted and blocked claims side by side,
                              with the reasons; the cover note to copy.
POST /drafts/<id>/edit        a new version of one accepted line (form: round,
                              section, position, text). The validator's
                              warnings are shown; they never block the save.

Drafted text and edits are escaped everywhere. A blocked claim is shown as
drafted, never reworded (hard rule 6), and can't be edited.
"""

import html
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine, text

from jhunt.api.page import page
from jhunt.platform.memory.claims import load_claim_levels
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists
from jhunt.workflows import application_pack

e = html.escape
SECTION_NAMES = {"cover_note": "Cover note sentence", "cv_line": "CV line"}
# Copy the cover note: the text area stays the fallback if this doesn't run.
COPY_SCRIPT = """<script>
document.getElementById("copy-note").addEventListener("click", function (event) {
  var area = document.getElementById("cover-note");
  navigator.clipboard.writeText(area.value).then(function () {
    event.target.textContent = "Copied";
  }, function () { area.select(); });
});
</script>"""


def add_routes(
    app: FastAPI,
    engine: Engine,
    registry: Registry,
    config_dir: Path,
    from_this_page: Callable[[Request], None],
) -> None:
    @app.post("/applications/{application_id}/pack")
    def draft(application_id: int, request: Request) -> RedirectResponse:
        from_this_page(request)
        try:
            application_pack.start_draft(engine, registry, application_id)
        except LookupError:
            raise HTTPException(404, "no such application") from None
        except ThreadExists:
            pass  # another request queued that number just now: it's on the page
        return RedirectResponse(f"/applications/{application_id}#pack", status_code=303)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def show(draft_id: int) -> str:
        found = _draft(engine, draft_id)
        if found is None:
            raise HTTPException(404, "no such draft")
        head, claims = found
        edits = application_pack.latest_edits(engine, draft_id)
        config = application_pack.load_pack_config(config_dir)
        body = _render(head, claims, edits, config)
        return page(f"Draft {draft_id}", body, current="/applications")

    @app.post("/drafts/{draft_id}/edit")
    def edit(
        draft_id: int,
        request: Request,
        round: int = Form(...),
        section: str = Form(...),
        position: int = Form(...),
        text: str = Form(...),
    ) -> RedirectResponse:
        from_this_page(request)
        try:
            application_pack.save_edit(
                engine, load_claim_levels(config_dir), draft_id=draft_id, round_=round,
                section=section, position=position, new_text=text,
            )  # fmt: skip
        except LookupError:
            raise HTTPException(404, "no such accepted line in this draft") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(f"/drafts/{draft_id}#{section}-{position}", status_code=303)


def _draft(engine: Engine, draft_id: int) -> tuple[Any, list[Any]] | None:
    with engine.connect() as conn:
        head = conn.execute(
            text(
                "SELECT d.id, d.application_id, d.model, d.created_at, a.company, a.role"
                " FROM pack_drafts d JOIN applications a ON a.id = d.application_id"
                " WHERE d.id = :d"
            ),
            {"d": draft_id},
        ).first()
        if head is None:
            return None
        claims = conn.execute(
            text(
                "SELECT round, section, position, text, fact_ids, blocked, problems, paragraph"
                " FROM draft_claims WHERE draft_id = :d ORDER BY round, section DESC, position"
            ),
            {"d": draft_id},
        ).all()
    return head, list(claims)


def _render(head: Any, claims: list[Any], edits: dict[Any, Any], config: Any) -> str:
    used = max((c.round for c in claims), default=0)
    final = [c for c in claims if c.round == used]
    accepted = [c for c in final if not c.blocked]

    def current(c: Any) -> str:
        edit: application_pack.Edit | None = edits.get((c.round, c.section, c.position))
        return edit.text if edit else str(c.text)

    note = application_pack.cover_note_text(
        application_pack.paragraphs(
            [(c.paragraph, current(c)) for c in accepted if c.section == "cover_note"]
        ),
        config,
        company=head.company,
        role=head.role,
    )
    lines = "".join(_line(c, edits.get((c.round, c.section, c.position)), head.id)
                    for c in accepted)  # fmt: skip
    blocked = [c for c in final if c.blocked]
    first_blocked = [c for c in claims if c.round == 0 and c.blocked] if used else []
    repaired = " · repaired" if used else ""
    return f"""
<h1>Draft {head.id}: {e(head.company)}, {e(head.role)}</h1>
<p class="meta">{e(head.model)} · {head.created_at:%Y-%m-%d %H:%M} UTC{repaired}
 · <a href="/applications/{head.application_id}">back to the application</a></p>
<div class="side-by-side">
<section class="accepted"><h2>Accepted</h2>
<h3>Cover note</h3>
<textarea id="cover-note" readonly rows="12">{e(note)}</textarea>
<p><button type="button" id="copy-note" class="primary">Copy cover note</button></p>
<h3>Lines</h3>
<ul class="plain lines">{lines}</ul>
</section>
<section class="blocked"><h2>Blocked</h2>
<p class="meta">Never in the note or the lines; never reworded.</p>
{_blocked("Still blocked" if used else "Blocked", blocked)}
{_blocked("Blocked in the first draft, before the repair", first_blocked) if used else ""}
</section>
</div>
{COPY_SCRIPT}"""


def _line(c: Any, edit: Any, draft_id: int) -> str:
    shown = edit.text if edit else c.text
    marker = (
        f' <span class="badge badge-new">edited by you · v{edit.version}</span>' if edit else ""
    )
    warnings = "".join(
        f'<p class="problem">Warning: {e(w)}</p>' for w in (edit.warnings if edit else ())
    )
    return f"""<li id="{c.section}-{c.position}">
<p><span class="tag">{SECTION_NAMES[c.section]}</span> {e(shown)}{marker}</p>
<p class="meta">Facts: {e(", ".join(c.fact_ids))}</p>{warnings}
<details><summary>Edit</summary>
<form method="post" action="/drafts/{draft_id}/edit">
<input type="hidden" name="round" value="{c.round}">
<input type="hidden" name="section" value="{c.section}">
<input type="hidden" name="position" value="{c.position}">
<textarea name="text" rows="3">{e(shown)}</textarea>
<p><button type="submit">Save as a new version</button></p></form></details></li>"""


def _blocked(title: str, claims: list[Any]) -> str:
    if not claims:
        return f"<h3>{title}</h3><p>None.</p>"
    items = "".join(
        f'<li><p><span class="tag tag-exclude">{SECTION_NAMES[c.section]}</span> {e(c.text)}</p>'
        + "".join(f'<p class="problem">{e(p)}</p>' for p in c.problems)
        + "</li>"
        for c in claims
    )
    return f'<h3>{title}</h3><ul class="plain">{items}</ul>'
