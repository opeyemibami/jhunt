"""The Facts page (ADR 0017, U6): each fact's latest version, to edit or retire.

GET  /facts                every fact's latest version, with an edit form
                           for each one that isn't retired.
POST /facts/<id>/edit      text, evidence, level, aliases (comma separated)
                           and the version the form showed: a new version,
                           checked as an import checks a file.
POST /facts/<id>/retire    the id typed back and the version shown.

Kind, status and verified date are left as they are: those change in your
facts file. A form from an old page is refused, so it never undoes a later
edit. Every value is escaped.
"""

import html
from collections.abc import Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine

from jhunt.api.page import page
from jhunt.platform.memory.facts import (
    LEVELS,
    FactChanged,
    FactsError,
    FactVersion,
    latest_facts,
    retire_fact,
    save_fact,
)

e = html.escape
EMPTY = "<p>No facts yet. Import your facts file with <code>jhunt-facts import</code>.</p>"


def add_routes(app: FastAPI, engine: Engine, from_this_page: Callable[[Request], None]) -> None:
    @app.get("/facts", response_class=HTMLResponse)
    def facts() -> str:
        latest = latest_facts(engine)
        citable = sum(f.status == "verified" for f in latest)
        rows = "".join(_row(f) for f in latest)
        table = (
            "<table><tr><th>Id</th><th>Kind</th><th>Level</th><th>Status</th><th>Text</th></tr>"
            f"{rows}</table>"
        )
        body = (
            f'<h1>Facts</h1><p class="meta">{len(latest)} facts · {citable} citable'
            " (verified, latest version)</p>"
            '<p class="meta">Each save is a new version; older ones are kept. Before you next'
            " import your facts file, run <code>jhunt-facts export FILE</code> so it has"
            " these edits.</p>" + (table if latest else EMPTY)
        )
        return page("Facts", body, current="/facts")

    @app.post("/facts/{fact_id}/edit")
    def edit(
        fact_id: str,
        request: Request,
        version: int = Form(...),
        text: str = Form(...),
        evidence: str = Form(""),
        level: str = Form(""),
        aliases: str = Form(""),
    ) -> RedirectResponse:
        from_this_page(request)
        changes = {
            "text": text,
            "evidence": evidence.strip() or None,
            "level": level or None,
            "aliases": [a.strip() for a in aliases.split(",") if a.strip()],
        }
        _saving(lambda: save_fact(engine, fact_id, changes, version=version))
        return RedirectResponse(f"/facts#fact-{fact_id}", status_code=303)

    @app.post("/facts/{fact_id}/retire")
    def retire(
        fact_id: str, request: Request, version: int = Form(...), typed: str = Form("")
    ) -> RedirectResponse:
        from_this_page(request)
        if typed.strip() != fact_id:
            raise HTTPException(400, f"type {fact_id} to retire it")
        _saving(lambda: retire_fact(engine, fact_id, via="browser", version=version))
        return RedirectResponse(f"/facts#fact-{fact_id}", status_code=303)


def _saving(save: Callable[[], object]) -> None:
    """The fact bank's refusals as HTTP errors, with every problem listed."""
    try:
        save()
    except LookupError:
        raise HTTPException(404, "no such fact") from None
    except FactChanged as exc:
        raise HTTPException(409, "; ".join(exc.problems)) from None
    except FactsError as exc:
        raise HTTPException(400, "; ".join(exc.problems)) from None


def _row(f: FactVersion) -> str:
    edited = " · edited in the browser" if f.via == "browser" else ""
    forms = "" if f.status == "retired" else _forms(f)
    return (
        f'<tr id="fact-{e(f.fact_id)}"><td><code>{e(f.fact_id)}</code></td><td>{e(f.kind)}</td>'
        f"<td>{e(f.level or '')}</td><td>{e(f.status)} · v{f.version}{edited}</td>"
        f"<td>{e(f.text)}{forms}</td></tr>"
    )


def _forms(f: FactVersion) -> str:
    skill = ""
    if f.kind == "skill":
        options = "".join(
            f'<option value="{lv}"{" selected" if lv == f.level else ""}>{lv}</option>'
            for lv in ("", *LEVELS)
        )
        skill = (
            f'<label>Level <select name="level">{options}</select></label>'
            '<label>Aliases, comma separated <input name="aliases"'
            f' value="{e(", ".join(f.aliases))}"></label>'
        )
    return f"""<details><summary>Edit</summary>
<form method="post" action="/facts/{e(f.fact_id)}/edit" class="fact-form">
<input type="hidden" name="version" value="{f.version}">
<label>Text <textarea name="text" rows="3">{e(f.text)}</textarea></label>
<label>Evidence <input name="evidence" value="{e(f.evidence or "")}"></label>
{skill}
<p><button type="submit" class="primary">Save as a new version</button></p></form>
<form method="post" action="/facts/{e(f.fact_id)}/retire" class="fact-form">
<input type="hidden" name="version" value="{f.version}">
<label>Type <code>{e(f.fact_id)}</code> to retire it <input name="typed"></label>
<p><button type="submit">Retire</button></p></form></details>"""
