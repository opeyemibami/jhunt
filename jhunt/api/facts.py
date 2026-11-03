"""The Facts page (ADR 0017): each fact's latest version, read only for now.
Editing, retiring and new versions come in U6. Every value is escaped."""

import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import Engine

from jhunt.api.page import page
from jhunt.platform.memory.facts import latest_facts

e = html.escape
EMPTY = "<p>No facts yet. Import your facts file with <code>jhunt-facts import</code>.</p>"


def add_routes(app: FastAPI, engine: Engine) -> None:
    @app.get("/facts", response_class=HTMLResponse)
    def facts() -> str:
        latest = latest_facts(engine)
        citable = sum(f.status == "verified" for f in latest)
        rows = "".join(
            f"<tr><td><code>{e(f.fact_id)}</code></td><td>{e(f.kind)}</td>"
            f"<td>{e(f.level or '')}</td><td>{e(f.status)} · v{f.version}</td>"
            f"<td>{e(f.text)}</td></tr>"
            for f in latest
        )
        table = (
            "<table><tr><th>Id</th><th>Kind</th><th>Level</th><th>Status</th><th>Text</th></tr>"
            f"{rows}</table>"
        )
        body = (
            f'<h1>Facts</h1><p class="meta">{len(latest)} facts · {citable} citable'
            " (verified, latest version)</p>" + (table if latest else EMPTY)
        )
        return page("Facts", body, current="/facts")
