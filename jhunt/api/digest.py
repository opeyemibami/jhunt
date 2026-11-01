"""The digest page (ADR 0012): Scout's in-scope postings, best first.

GET /  in-scope postings with a title match, by fit score, with what matched;
       the other in-scope postings under a collapsed "Other in-scope jobs";
       a count of the out-of-scope ones, which are never listed.
POST /postings/<id>/capture  queues a Capture run for that posting, read from
       its board's API, and returns to the page. A running jhunt-worker does it.

There is no login yet, so the server listens on 127.0.0.1 only (jhunt.app.web)
and checks two headers. Host must name this machine and port, so a website
whose DNS name points at 127.0.0.1 can't read the page (DNS rebinding).
Origin must be this page on a capture, so another site open in the browser
can't post one. Board text is data: every value is escaped.
"""

import contextlib
import html
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import Engine, text

from jhunt.jobs.boards import board_job_url
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import capture

# The order matches are shown in. jsonb doesn't keep the order they were written in.
_MATCH_SECTIONS = ("title", "include", "penalty", "exclude")


def create_app(engine: Engine, registry: Registry, *, port: int) -> FastAPI:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{host}" for host in hosts}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def only_this_host(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host") not in hosts:
            return Response("unknown host", status_code=400)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def digest() -> str:
        return _render(**_load(engine))

    @app.post("/postings/{posting_id}/capture")
    def capture_posting(posting_id: int, request: Request) -> RedirectResponse:
        if request.headers.get("origin") not in origins:
            raise HTTPException(403, "capture only from the digest page")
        _start_capture(engine, registry, posting_id)
        return RedirectResponse(f"/#posting-{posting_id}", status_code=303)

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
                " p.fit_score, p.fit_matches, a.id AS application_id"
                " FROM postings p LEFT JOIN applications a ON a.dedupe_key = p.dedupe_key"
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
                    "SELECT DISTINCT ON (thread_id) thread_id, status, error FROM runs"
                    " WHERE thread_id = ANY(:threads) ORDER BY thread_id, id DESC"
                ),
                {"threads": threads},
            )
        }
        scout = conn.execute(
            text(
                "SELECT thread_id, status, created_at FROM runs WHERE workflow = 'scout'"
                " ORDER BY id DESC LIMIT 1"
            )
        ).first()
    rows = [(p, captures.get(t)) for p, t in zip(postings, threads, strict=True)]
    return {
        "matched": [r for r in rows if "title" in (r[0].fit_matches or {})],
        "other": [r for r in rows if "title" not in (r[0].fit_matches or {})],
        "hidden": hidden,
        "scout": scout,
    }


def _render(matched: list[Any], other: list[Any], hidden: int, scout: Any) -> str:
    e = html.escape
    last = (
        f"Last Scout run: {e(scout.thread_id)}, {e(scout.status)},"
        f" {scout.created_at:%Y-%m-%d %H:%M} UTC"
        if scout is not None
        else "Scout hasn't run yet"
    )
    out_of_scope = f"{hidden} out-of-scope posting{'' if hidden == 1 else 's'} hidden"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scout digest</title>
<style>{_CSS}</style></head>
<body><main>
<h1>Scout digest</h1>
<p class="meta">{len(matched)} title matches · {len(other)} other in scope · {out_of_scope}</p>
<p class="meta">{last}</p>
<ol class="postings">{"".join(_row(p, run) for p, run in matched)}</ol>
<details><summary>Other in-scope jobs ({len(other)})</summary>
<ol class="postings">{"".join(_row(p, run) for p, run in other)}</ol>
</details>
</main></body></html>
"""


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
        return f"Captured: application {p.application_id}"
    if run is None:
        return (
            f'<form method="post" action="/postings/{p.id}/capture">'
            '<button type="submit">Capture</button></form>'
        )
    if run.status == "failed":
        return f"Capture failed: {html.escape(run.error or '')}"
    return f"Capture {html.escape(run.status)}"


_CSS = """
:root { --bg: #fff; --fg: #1c1c1c; --muted: #666; --line: #e3e3e3; --accent: #1d4ed8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #151515; --fg: #e8e8e8; --muted: #9a9a9a; --line: #2c2c2c; --accent: #8ab4ff; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
.meta { color: var(--muted); margin: .15rem 0; }
ol.postings { list-style: none; padding: 0; margin: 1rem 0; }
ol.postings li { border-top: 1px solid var(--line); padding: .7rem 0; }
.head { display: flex; gap: .6rem; align-items: baseline; }
.score { font-variant-numeric: tabular-nums; font-weight: 600; min-width: 2.6rem; }
a.title { color: var(--accent); text-decoration: none; font-weight: 600; }
a.title:hover { text-decoration: underline; }
span.title { font-weight: 600; }
li .meta, .matched, .action { margin-left: 3.2rem; }
.matched { font-size: .9rem; }
.action { margin-top: .3rem; font-size: .9rem; color: var(--muted); }
button { font: inherit; padding: .2rem .8rem; cursor: pointer; }
details { margin-top: 1.5rem; }
summary { cursor: pointer; font-weight: 600; }
"""
