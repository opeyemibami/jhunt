"""The digest page (ADR 0012): Scout's in-scope postings, best first.

GET /  in-scope postings with a title match, by fit score, with what matched;
       the other in-scope postings under a collapsed "Other in-scope jobs";
       a count of the out-of-scope ones, which are never listed. A captured
       posting shows its application and its latest submission (ADR 0013).
POST /postings/<id>/capture  queues a Capture run for that posting, read from
       its board's API, and returns to the page. A running jhunt-worker does it.
POST /scout/run, /scout/retry, /scout/rescore  queue today's Scout run under
       the same rules as jhunt-scout (ADR 0011, ADR 0017).
GET /runs/<id>  a run's status as JSON, for the live status script (live.py).

There is no login yet, so the server listens on 127.0.0.1 only (jhunt.app.web)
and checks two headers. Host must name this machine and port, so a website
whose DNS name points at 127.0.0.1 can't read the page (DNS rebinding).
Origin must be this page on every form post, so another site open in the
browser can't post one. Board text is data: every value is escaped.
"""

import contextlib
import html
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import Engine, text

from jhunt.api import applications, live
from jhunt.api.page import page
from jhunt.jobs.boards import board_job_url
from jhunt.platform.artifacts import RecordStore
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import capture, scout

# The order matches are shown in. jsonb doesn't keep the order they were written in.
_MATCH_SECTIONS = ("title", "include", "penalty", "exclude")


def create_app(
    engine: Engine,
    registry: Registry,
    *,
    port: int,
    config_dir: Path,
    conninfo: str,
    records: RecordStore,
    today: Callable[[], str] = scout.utc_today,
) -> FastAPI:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # The last reason Run Scout now couldn't start (a bad config file), shown
    # on the panel until the next try.
    problems: dict[str, str] = {}

    def from_this_page(request: Request) -> None:
        if request.headers.get("origin") not in origins:
            raise HTTPException(403, "only from the digest page")

    @app.middleware("http")
    async def only_this_host(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host") not in hosts:
            return Response("unknown host", status_code=400)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def digest() -> str:
        latest = scout.latest_today(engine, today())
        found = (
            None if latest is None else live.run_status(engine, conninfo, registry, latest.run_id)
        )
        shown = "" if found is None else found["text"]
        return _render(**_load(engine), latest=latest, shown=shown, problem=problems.get("scout"))

    @app.post("/postings/{posting_id}/capture")
    def capture_posting(posting_id: int, request: Request) -> RedirectResponse:
        from_this_page(request)
        _start_capture(engine, registry, posting_id)
        return RedirectResponse(f"/#posting-{posting_id}", status_code=303)

    @app.post("/scout/{mode}")
    def run_scout(mode: str, request: Request) -> RedirectResponse:
        from_this_page(request)
        if mode not in ("run", "retry", "rescore"):
            raise HTTPException(404, "no such Scout action")
        problems.pop("scout", None)
        try:
            scout.start_scout(engine, registry, config_dir, today(), mode)  # type: ignore[arg-type]
        except scout.ScoutRefused:
            pass  # the panel shows today's run, which is why
        except (scout.SourcesError, scout.ScopeError, scout.TargetsError) as exc:
            problems["scout"] = str(exc)
        return RedirectResponse("/#scout", status_code=303)

    applications.add_routes(app, engine, records)

    @app.get("/runs/{run_id}")
    def run_status(run_id: int) -> JSONResponse:
        found = live.run_status(engine, conninfo, registry, run_id)
        if found is None:
            raise HTTPException(404, "no such run")
        return JSONResponse(found)

    return app


def _start_capture(engine: Engine, registry: Registry, posting_id: int) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT provider, board, job_id, dedupe_key, company, title FROM postings"
                " WHERE id = :id AND in_scope"
            ),
            {"id": posting_id},
        ).first()
    if row is None:
        raise HTTPException(404, "no such in-scope posting")
    # The board's own link, never one sent by the browser: Capture reads the
    # job from the board API under the posting's key (ADR 0007, ADR 0012).
    url = board_job_url(row.provider, row.board, row.job_id)
    key = capture.capture_key(url)
    if key != row.dedupe_key:
        raise RuntimeError(f"posting {posting_id}: its board link has another key")
    # Already queued or captured: nothing to do; the page says which.
    with contextlib.suppress(ThreadExists):
        enqueue_start(
            engine,
            registry,
            capture.thread_id_for(key),
            capture.WORKFLOW,
            # The posting's company and title fill what the board doesn't
            # name (Ashby has no company); the board's own values win.
            {"url": url, "company": row.company, "title": row.title, "dedupe_key": key},
        )


def _load(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        postings = conn.execute(
            text(
                "SELECT p.id, p.dedupe_key, p.url, p.title, p.company, p.scope_reason,"
                " p.fit_score, p.fit_matches, a.id AS application_id,"
                " s.submitted_at, s.channel AS submitted_via"
                " FROM postings p LEFT JOIN applications a ON a.dedupe_key = p.dedupe_key"
                # The latest submission counts (ADR 0013).
                " LEFT JOIN LATERAL (SELECT submitted_at, channel FROM submissions"
                "  WHERE application_id = a.id ORDER BY submitted_at DESC, id DESC LIMIT 1) s"
                " ON true"
                " WHERE p.in_scope"
                " ORDER BY p.fit_score DESC NULLS LAST, p.title, p.id"
            )
        ).all()
        hidden = conn.execute(text("SELECT count(*) FROM postings WHERE NOT in_scope")).scalar_one()
        threads = [capture.thread_id_for(p.dedupe_key) for p in postings]
        # Each posting's latest Capture run, if any.
        captures = {
            r.thread_id: r
            for r in conn.execute(
                text(
                    "SELECT DISTINCT ON (thread_id) id, thread_id, status, error FROM runs"
                    " WHERE thread_id = ANY(:threads) ORDER BY thread_id, id DESC"
                ),
                {"threads": threads},
            )
        }
    rows = [(p, captures.get(t)) for p, t in zip(postings, threads, strict=True)]
    return {
        "matched": [r for r in rows if "title" in (r[0].fit_matches or {})],
        "other": [r for r in rows if "title" not in (r[0].fit_matches or {})],
        "hidden": hidden,
    }


def _render(
    matched: list[Any],
    other: list[Any],
    hidden: int,
    latest: scout.Today | None,
    shown: str,
    problem: str | None,
) -> str:
    out_of_scope = f"{hidden} out-of-scope posting{'' if hidden == 1 else 's'} hidden"
    active = (latest is not None and latest.status in live.ACTIVE) or any(
        run is not None and run.status in live.ACTIVE for _, run in matched + other
    )
    body = f"""
<h1>Scout digest</h1>
<p class="meta">{len(matched)} title matches · {len(other)} other in scope · {out_of_scope}</p>
{_scout_panel(latest, shown, problem)}
<ol class="postings">{"".join(_row(p, run) for p, run in matched)}</ol>
<details><summary>Other in-scope jobs ({len(other)})</summary>
<ol class="postings">{"".join(_row(p, run) for p, run in other)}</ol>
</details>"""
    return page("Scout digest", body, waiting=active)


def _scout_panel(latest: scout.Today | None, shown: str, problem: str | None) -> str:
    """Today's Scout run, live, and the one action its state allows (ADR 0011):
    Run Scout now with no run today, Retry after a failure, Rescore after a
    success, nothing while it's queued or running."""

    def button(mode: str, label: str) -> str:
        return (
            f'<form method="post" action="/scout/{mode}" class="inline">'
            f'<button type="submit">{label}</button></form>'
        )

    lines = []
    if problem:
        lines.append(f'<p class="problem">Scout couldn\'t start: {html.escape(problem)}</p>')
    if latest is None:
        lines.append(f"<p>No Scout run today. {button('run', 'Run Scout now')}</p>")
    else:
        state = live.status_element(latest.run_id, latest.status, shown)
        action = {"failed": button("retry", "Retry"), "succeeded": button("rescore", "Rescore")}
        lines.append(
            f"<p>Today's run ({html.escape(latest.thread_id)}): {state}"
            f" {action.get(latest.status, '')}</p>"
        )
    return f'<section id="scout" class="scout"><h2>Scout</h2>{"".join(lines)}</section>'


def _row(p: Any, run: Any) -> str:
    e = html.escape
    score = "-" if p.fit_score is None else f"{p.fit_score:.2f}"
    title = e(p.title)
    # Only web links are links: a board's text could hold javascript: or worse.
    if urlsplit(p.url).scheme in ("http", "https"):
        title_html = f'<a class="title" href="{e(p.url)}" rel="noreferrer">{title}</a>'
    else:
        title_html = f'<span class="title">{title}</span>'
    found = p.fit_matches or {}
    matched = " · ".join(
        f"{section}: {e(', '.join(found[section]))}"
        for section in _MATCH_SECTIONS
        if found.get(section)
    )
    return f"""
<li id="posting-{p.id}">
<div class="head"><span class="score">{score}</span>{title_html}</div>
<div class="meta">{e(p.company)} · {e(p.scope_reason)}</div>
<div class="matched">{matched}</div>
<div class="action">{_action(p, run)}</div>
</li>"""


def _action(p: Any, run: Any) -> str:
    if p.application_id is not None:
        link = f'<a href="/applications/{p.application_id}">application {p.application_id}</a>'
        captured = f"Captured: {link}"
        if p.submitted_at is None:
            return captured
        submitted = f"Submitted {p.submitted_at:%Y-%m-%d} via {html.escape(p.submitted_via)}"
        return f"{captured} · {submitted}"
    if run is None:
        return (
            f'<form method="post" action="/postings/{p.id}/capture">'
            '<button type="submit">Capture</button></form>'
        )
    if run.status == "failed":
        return f"Capture failed: {html.escape(run.error or '')}"
    # Queued or running: the live script updates it in place (ADR 0017).
    return live.status_element(run.id, run.status, run.status, label="Capture ")
