"""The applications list and an application's page (ADR 0017, U3).

GET /applications       every application: company, role, stage, when it was
                        captured, its submissions and its latest draft.
GET /applications/archived  the archived ones.
GET /applications/<id>  one application: its stored job description, stage,
                        events, submissions (with their documents) and drafts.
POST /applications/<id>/archive/<outcome>  sets the outcome (withdrawn or
                        not_pursuing) and moves it out of the main list;
POST /applications/<id>/restore  clears it again. Each appends an event.

The job description is read from records directly (RecordStore.get), by
exact generation with its sha256 checked: the read-only exception in ADR 0017.
It's only displayed; nothing is decided on it. If the read fails, the page
says why and shows everything else. Every value is escaped.
"""

import html
import json
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine, text

from jhunt.api import live, pack
from jhunt.api.page import page
from jhunt.platform.artifacts import RecordIntegrityError, RecordStore

e = html.escape
# Why an application was archived: the outcome it's given, and how it's shown.
OUTCOMES = {"withdrawn": "withdrawn", "not_pursuing": "not pursuing"}


def add_routes(
    app: FastAPI, engine: Engine, records: RecordStore, from_this_page: Callable[[Request], None]
) -> None:
    @app.get("/applications", response_class=HTMLResponse)
    def applications() -> str:
        rows = _applications(engine)
        active = [r for r in rows if r.outcome is None]
        archived = len(rows) - len(active)
        return page("Applications", _list(active, archived), current="/applications")

    @app.get("/applications/archived", response_class=HTMLResponse)
    def archived() -> str:
        rows = [r for r in _applications(engine) if r.outcome is not None]
        return page("Archived", _list(rows, None), current="/applications")

    @app.post("/applications/{application_id}/archive/{outcome}")
    def archive(application_id: int, outcome: str, request: Request) -> RedirectResponse:
        from_this_page(request)
        if outcome not in OUTCOMES:
            raise HTTPException(404, "no such outcome")
        _set_outcome(engine, application_id, outcome)
        return RedirectResponse(f"/applications/{application_id}", status_code=303)

    @app.post("/applications/{application_id}/restore")
    def restore(application_id: int, request: Request) -> RedirectResponse:
        from_this_page(request)
        _set_outcome(engine, application_id, None)
        return RedirectResponse(f"/applications/{application_id}", status_code=303)

    @app.get("/applications/{application_id}", response_class=HTMLResponse)
    def application(application_id: int) -> str:
        found = _application(engine, application_id)
        if found is None:
            raise HTTPException(404, "no such application")
        job = _job_description(records, found)
        body, waiting = _detail(engine, found, job)
        return page(f"{found.company}, {found.role}", body, current="/applications",
                    waiting=waiting)  # fmt: skip


def _applications(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT a.id, a.company, a.role, a.stage, a.outcome, a.opened_at,"
                    " (SELECT count(*) FROM submissions s WHERE s.application_id = a.id)"
                    "   AS submissions,"
                    " (SELECT max(submitted_at) FROM submissions s"
                    "   WHERE s.application_id = a.id) AS last_submitted,"
                    " d.id AS draft_id, d.accepted, d.blocked"
                    " FROM applications a"
                    # The latest draft and, from the round it uses, its counts.
                    " LEFT JOIN LATERAL ("
                    "   SELECT p.id,"
                    "    count(*) FILTER (WHERE NOT c.blocked) AS accepted,"
                    "    count(*) FILTER (WHERE c.blocked) AS blocked"
                    "   FROM pack_drafts p JOIN draft_claims c ON c.draft_id = p.id"
                    "   WHERE p.application_id = a.id AND c.round = (SELECT max(round)"
                    "     FROM draft_claims WHERE draft_id = p.id)"
                    "   GROUP BY p.id ORDER BY p.id DESC LIMIT 1) d ON true"
                    " ORDER BY a.opened_at DESC, a.id DESC"
                )
            )
        )


def _set_outcome(engine: Engine, application_id: int, outcome: str | None) -> None:
    """Archive (an outcome) or restore (none), with its event, in one transaction."""
    with engine.begin() as conn:
        changed = conn.execute(
            text(
                "UPDATE applications SET outcome = CAST(:o AS text),"
                " closed_at = CASE WHEN CAST(:o AS text) IS NULL THEN NULL ELSE now() END"
                " WHERE id = :a"
            ),
            {"o": outcome, "a": application_id},
        ).rowcount
        if not changed:
            raise HTTPException(404, "no such application")
        kind, payload = ("archived", {"outcome": outcome}) if outcome else ("restored", {})
        conn.execute(
            text(
                "INSERT INTO events (application_id, type, payload, actor)"
                " VALUES (:a, :t, CAST(:p AS jsonb), 'you')"
            ),
            {"a": application_id, "t": kind, "p": json.dumps(payload)},
        )


def _list(rows: list[Any], archived: int | None) -> str:
    """The main list (archived: how many are archived) or the archived one (None)."""
    title = "Applications" if archived is not None else "Archived applications"
    more = f' · <a href="/applications/archived">{archived} archived</a>' if archived else ""
    if not rows:
        empty = (
            "No applications yet. Capture a posting from the digest."
            if archived is not None
            else "Nothing archived."
        )
        return f'<h1>{title}</h1><p class="meta">{more.removeprefix(" · ")}</p><p>{empty}</p>'
    body = []
    for r in rows:
        submitted = (
            f"{r.submissions} submission{'' if r.submissions == 1 else 's'},"
            f" last {r.last_submitted:%Y-%m-%d}"
            if r.submissions
            else "not submitted"
        )
        drafted = (
            f"draft {r.draft_id}: {r.accepted} accepted, {r.blocked} blocked"
            if r.draft_id is not None
            else "no draft"
        )
        body.append(
            f'<tr><td><a href="/applications/{r.id}">{e(r.company)}</a></td><td>{e(r.role)}</td>'
            f"<td>{e(OUTCOMES.get(r.outcome or '', r.stage))}</td>"
            f'<td class="nowrap">{r.opened_at:%Y-%m-%d}</td>'
            f"<td>{submitted}</td><td>{drafted}</td></tr>"
        )
    return (
        f'<h1>{title}</h1><p class="meta">{len(rows)} applications{more}</p>'
        "<table><tr><th>Company</th><th>Role</th><th>Stage</th><th>Captured</th>"
        "<th>Submissions</th><th>Latest draft</th></tr>" + "".join(body) + "</table>"
    )


def _application(engine: Engine, application_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT a.id, a.company, a.role, a.stage, a.outcome, a.opened_at, s.url,"
                " s.text_uri,"
                " s.text_generation, s.text_sha256, s.captured_at"
                " FROM applications a JOIN jd_snapshots s ON s.id = a.snapshot_id"
                " WHERE a.id = :id"
            ),
            {"id": application_id},
        ).first()


def _job_description(records: RecordStore, found: Any) -> tuple[str | None, str | None]:
    """(text, None), or (None, why it can't be shown). Never unchecked bytes."""
    uri = found.text_uri
    if not uri.startswith("gs://") or "/" not in uri[5:]:
        return None, f"unexpected snapshot address {uri!r}"
    key = uri[5:].split("/", 1)[1]
    try:
        data = records.get(key, found.text_generation, found.text_sha256)
    except RecordIntegrityError as exc:
        return None, f"its stored bytes don't match the sha256 recorded at capture ({exc})"
    except Exception as exc:  # storage down or the object missing: say so, show the rest
        return None, f"{type(exc).__name__}: {exc}"
    return data.decode("utf-8", errors="replace"), None


def _detail(engine: Engine, a: Any, job: tuple[str | None, str | None]) -> tuple[str, bool]:
    with engine.connect() as conn:
        events = conn.execute(
            text("SELECT type, actor, at FROM events WHERE application_id = :a ORDER BY at, id"),
            {"a": a.id},
        ).all()
        submissions = conn.execute(
            text(
                "SELECT id, channel, submitted_at FROM submissions WHERE application_id = :a"
                " ORDER BY submitted_at DESC, id DESC"
            ),
            {"a": a.id},
        ).all()
        documents = conn.execute(
            text(
                "SELECT sd.submission_id, d.kind, d.version, d.file_name, d.size, d.sha256"
                " FROM submission_documents sd JOIN documents d ON d.id = sd.document_id"
                " WHERE d.application_id = :a ORDER BY d.kind, d.version"
            ),
            {"a": a.id},
        ).all()
        submission_run = conn.execute(
            text(
                "SELECT id, status, error FROM runs WHERE workflow = 'submission'"
                " AND thread_id LIKE :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": f"submission-{a.id}-%"},
        ).first()
        pack_run = conn.execute(
            text(
                "SELECT id, status FROM runs WHERE workflow = 'application_pack'"
                " AND thread_id LIKE :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": f"pack-{a.id}-%"},
        ).first()
        drafts = conn.execute(
            text(
                "SELECT p.id, p.thread_id, p.model, p.created_at, max(c.round) AS rounds,"
                " count(*) FILTER (WHERE NOT c.blocked AND c.round = r.used) AS accepted,"
                " count(*) FILTER (WHERE c.blocked AND c.round = r.used) AS blocked"
                " FROM pack_drafts p JOIN draft_claims c ON c.draft_id = p.id"
                " JOIN LATERAL (SELECT max(round) AS used FROM draft_claims"
                "   WHERE draft_id = p.id) r ON true"
                " WHERE p.application_id = :a GROUP BY p.id, r.used ORDER BY p.id DESC"
            ),
            {"a": a.id},
        ).all()

    link = (
        f'<a href="{e(a.url)}" rel="noreferrer">{e(a.url)}</a>'
        if urlsplit(a.url).scheme in ("http", "https")
        else e(a.url)
    )
    text_, problem = job
    shown = (
        f'<pre class="job">{e(text_)}</pre>'
        if text_ is not None
        else f'<p class="problem">Can\'t show the job description: {e(problem or "")}</p>'
    )
    event_items = "".join(
        f"<li>{ev.at:%Y-%m-%d %H:%M} · {e(ev.type)} · {e(ev.actor)}</li>" for ev in events
    )
    submission_items = []
    for s in submissions:
        docs = "".join(
            f"<li>{e(d.kind)} v{d.version}: {e(d.file_name)} ({d.size} bytes,"
            f" sha256 {e(d.sha256[:12])}…)</li>"
            for d in documents
            if d.submission_id == s.id
        )
        submission_items.append(
            f"<li>Submission {s.id}: {s.submitted_at:%Y-%m-%d} via {e(s.channel)}"
            f'<ul class="plain">{docs}</ul></li>'
        )
    draft_items = "".join(
        f'<li><a href="/drafts/{d.id}">Draft {d.id}</a> · {d.created_at:%Y-%m-%d %H:%M}'
        f" · {e(d.model)} ·"
        f" {d.accepted} accepted, {d.blocked} blocked"
        f"{' · repaired' if d.rounds else ''}"
        # The latest draft's requirements match, in one line (ADR 0019).
        + (f" · {e(pack.requirements_summary(engine, d.thread_id))}" if n == 0 else "")
        + "</li>"
        for n, d in enumerate(drafts)
    )
    return (
        f"""
<h1>{e(a.company)}, {e(a.role)}</h1>
<p class="meta">Stage: {e(a.stage)} · captured {a.captured_at:%Y-%m-%d %H:%M} UTC · {link}</p>
{_archive_panel(a)}
<h2>Job description</h2>
{shown}
<h2>Events</h2>
<ul class="plain">{event_items or "<li>None</li>"}</ul>
<h2 id="submissions">Submissions</h2>
<ul class="plain">{"".join(submission_items) or "<li>None yet</li>"}</ul>
{_submission_form(a.id, submission_run)}
<h2 id="pack">Drafts</h2>
<p><form method="post" action="/applications/{a.id}/pack" class="inline">
<button type="submit" class="primary">Draft pack</button></form> {_pack_status(pack_run)}</p>
<ul class="plain">{draft_items or "<li>None yet</li>"}</ul>""",
        any(r is not None and r.status in live.ACTIVE for r in (pack_run, submission_run)),
    )


def _archive_panel(a: Any) -> str:
    """Archive with an outcome, or, once archived, the outcome and Restore."""

    def button(path: str, label: str) -> str:
        return (
            f'<form method="post" action="/applications/{a.id}/{path}" class="inline">'
            f'<button type="submit">{label}</button></form>'
        )

    if a.outcome is not None:
        shown = OUTCOMES.get(a.outcome, a.outcome)
        return f'<p class="meta">Outcome: {e(shown)} · {button("restore", "Restore")}</p>'
    archive = " ".join(
        button(f"archive/{key}", f"Archive: {label}") for key, label in OUTCOMES.items()
    )
    return f'<p class="meta">{archive}</p>'


def _submission_form(application_id: int, run: Any) -> str:
    """Record what you sent (ADR 0018): files, channel and date, then a preview.
    The latest recording's status, live, while it runs or if it failed."""
    status = ""
    if run is not None and (run.status in live.ACTIVE or run.status == "failed"):
        shown = f"failed: {run.error}" if run.status == "failed" else run.status
        status = live.status_element(run.id, run.status, shown, label="Recording: ")
    file_input = (
        '<label>{} <input type="file" name="{}" accept=".pdf,.docx,.odt,.txt,.md"{}></label>'
    )
    return f"""<details{" open" if status else ""}><summary>Record a submission</summary>
<p class="meta">You submit outside jhunt; this records exactly what you sent. PDF, DOCX, ODT,
TXT or MD, up to 10 MB each; answers as TXT or MD.</p>
<form method="post" action="/applications/{application_id}/submission"
 enctype="multipart/form-data" class="fact-form">
{file_input.format("CV", "cv", "")}
{file_input.format("Cover note", "cover_note", "")}
{file_input.format("Answers", "answers", "")}
{file_input.format("Other files", "other", " multiple")}
<label>Channel, e.g. company site <input name="channel"></label>
<label>Submitted on
<input type="date" name="submitted_at" value="{date.today().isoformat()}"></label>
<p><button type="submit" class="primary">Preview</button></p></form></details>
<p>{status}</p>"""


def _pack_status(run: Any) -> str:
    """The latest draft run while it's queued or running, live (ADR 0017)."""
    if run is None or run.status not in live.ACTIVE:
        return ""
    return live.status_element(run.id, run.status, run.status, label="Drafting: ")
