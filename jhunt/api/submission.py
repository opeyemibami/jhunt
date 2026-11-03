"""Recording a submission from the application page (ADR 0017 U5, ADR 0018).

POST /applications/<id>/submission          the files (cv, cover_note,
    answers, other), channel and date: every file is checked first (type,
    size, name), and if any problem is found nothing is stored. Then each is
    staged at uploads/<sha256>/<file name> through the gateway, and the
    preview shows each file's name, size and sha256.
POST /applications/<id>/submission/confirm  the preview's files, channel and
    date, with the application id typed back: queues the submission run,
    whose status shows live on the application page.

You submit outside jhunt; this records exactly what you sent, locked once
recorded (ADR 0013). Every value shown is escaped.
"""

import base64
import contextlib
import hashlib
import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine, text

from jhunt.api.page import page
from jhunt.platform.artifacts import StagingStore
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import workspace
from jhunt.workflows.workspace import submission

e = html.escape
KIND_NAMES = {"cv": "CV", "cover_note": "Cover note", "answers": "Answers", "other": "Other"}
SHA256 = re.compile(r"[0-9a-f]{64}")
FILE_FIELDS = ("kind", "file_name", "content_type", "size", "sha256", "key", "generation")


def add_routes(
    app: FastAPI,
    engine: Engine,
    registry: Registry,
    staging: StagingStore | None,
    from_this_page: Callable[[Request], None],
) -> None:
    gateway = (
        ToolGateway(engine, [workspace.stage_submission_file_tool(staging)]) if staging else None
    )

    @app.post("/applications/{application_id}/submission", response_class=HTMLResponse)
    def preview(
        application_id: int,
        request: Request,
        channel: str = Form(""),
        submitted_at: str = Form(""),
        cv: Annotated[UploadFile | None, File()] = None,
        cover_note: Annotated[UploadFile | None, File()] = None,
        answers: Annotated[UploadFile | None, File()] = None,
        other: Annotated[list[UploadFile] | None, File()] = None,
    ) -> HTMLResponse:
        from_this_page(request)
        found = _application(engine, application_id)
        given = [("cv", cv), ("cover_note", cover_note), ("answers", answers)]
        uploads = [
            (k, u) for k, u in given + [("other", u) for u in other or []] if u and u.filename
        ]

        # Everything is checked before anything is read into memory or staged.
        problems = []
        for kind, upload in uploads:
            size = upload.size if upload.size is not None else len(upload.file.read())
            problem = submission.file_problem(kind, str(upload.filename), size)
            if problem is not None:
                problems.append(problem)
        if not uploads:
            problems.append("nothing to record: give at least one file")
        if not channel.strip():
            problems.append("say where you sent it (the channel)")
        try:
            when = submission.submitted_at_utc(submitted_at)
        except ValueError as exc:
            problems.append(f"date: {exc}")
        files: list[dict[str, Any]] = []
        if not problems:
            for kind, upload in uploads:
                upload.file.seek(0)
                data = upload.file.read()
                name = str(upload.filename)
                content_type = submission.CONTENT_TYPES[Path(name).suffix.lower()]
                files.append({"kind": kind, "file_name": name, "size": len(data), "data": data,
                              "sha256": hashlib.sha256(data).hexdigest(),
                              "content_type": content_type})  # fmt: skip
            if len({f["sha256"] for f in files}) < len(files):
                problems.append("the same file is given twice")
        if problems:
            items = "".join(f"<li>{e(p)}</li>" for p in problems)
            body = (
                f"<h1>Not recorded</h1><p>Nothing was stored. Fix these and try again:</p>"
                f'<ul class="problem">{items}</ul>'
                f'<p><a href="/applications/{application_id}#submissions">Back</a></p>'
            )
            return HTMLResponse(page("Not recorded", body, current="/applications"), 400)

        thread_id = submission.submission_thread_id(
            application_id, [f["sha256"] for f in files], channel.strip(), when
        )
        if (recorded := _recorded(engine, thread_id)) is not None or _started(engine, thread_id):
            what = f"submission {recorded}" if recorded else "this submission, being recorded now"
            body = (
                f"<h1>Already recorded</h1><p>These files, channel and date are {what}.</p>"
                f'<p><a href="/applications/{application_id}#submissions">Back</a></p>'
            )
            return HTMLResponse(page("Already recorded", body, current="/applications"))
        if gateway is None:
            raise HTTPException(503, "set GCS_STAGING_BUCKET: uploads wait there (ADR 0018)")
        for f in files:
            data = f.pop("data")
            staged = gateway.call(
                thread_id, "stage", f"{f['kind']}:{f['sha256']}:{f['file_name']}",
                "stage_submission_file",
                {"kind": f["kind"], "file_name": f["file_name"], "sha256": f["sha256"],
                 "data": base64.b64encode(data).decode()},
                run_id=None,
            )  # fmt: skip
            f.update(key=staged["key"], generation=staged["generation"])
        return HTMLResponse(page("Confirm the submission", _preview(found, files, channel, when),
                                 current="/applications"))  # fmt: skip

    @app.post("/applications/{application_id}/submission/confirm")
    def confirm(
        application_id: int,
        request: Request,
        files: str = Form(...),
        channel: str = Form(...),
        submitted_at: str = Form(...),
        typed: str = Form(""),
    ) -> RedirectResponse:
        from_this_page(request)
        _application(engine, application_id)
        if typed.strip() != str(application_id):
            raise HTTPException(400, f"type {application_id} to confirm; nothing recorded")
        confirmed = _confirmed_files(files)
        try:
            when = submission.submitted_at_utc(submitted_at)
        except ValueError as exc:
            raise HTTPException(400, f"date: {exc}") from None
        thread_id = submission.submission_thread_id(
            application_id, [f["sha256"] for f in confirmed], channel.strip(), when
        )
        if _recorded(engine, thread_id) is None:
            payload = {"application_id": application_id, "channel": channel.strip(),
                       "submitted_at": when, "files": confirmed}  # fmt: skip
            # Confirmed twice: the first is being recorded.
            with contextlib.suppress(ThreadExists):
                enqueue_start(engine, registry, thread_id, workspace.WORKFLOW, payload)
        return RedirectResponse(f"/applications/{application_id}#submissions", status_code=303)


def _preview(a: Any, files: list[dict[str, Any]], channel: str, when: str) -> str:
    rows = "".join(
        f"<tr><td>{KIND_NAMES[f['kind']]}</td><td>{e(f['file_name'])}</td>"
        f'<td class="nowrap">{f["size"]} bytes</td><td><code>{f["sha256"]}</code></td></tr>'
        for f in files
    )
    return f"""
<h1>Confirm the submission</h1>
<p class="meta">Application {a.id}: {e(a.company)}, {e(a.role)}</p>
<table><tr><th>Kind</th><th>File</th><th>Size</th><th>sha256</th></tr>{rows}</table>
<p>Channel: {e(channel.strip())} · submitted: {e(when[:16].replace("T00:00", ""))}</p>
<p><strong>This is locked once recorded</strong>: it can't be edited, only followed by
another submission. The sha256 above is what will be recorded.</p>
<form method="post" action="/applications/{a.id}/submission/confirm" class="fact-form">
<input type="hidden" name="files" value="{e(json.dumps(files))}">
<input type="hidden" name="channel" value="{e(channel.strip())}">
<input type="hidden" name="submitted_at" value="{e(when)}">
<label>Type the application id, {a.id}, to confirm <input name="typed"></label>
<p><button type="submit" class="primary">Record this submission</button>
<a href="/applications/{a.id}#submissions">Cancel</a></p></form>"""


def _confirmed_files(raw: str) -> list[dict[str, Any]]:
    """The preview's files, as the preview showed them, or a 400."""
    try:
        files = json.loads(raw)
    except ValueError:
        files = None
    if not isinstance(files, list) or not files or not all(_preview_file(f) for f in files):
        raise HTTPException(400, "not a preview's files; preview again")
    return files


def _preview_file(f: Any) -> bool:
    """One file as the preview shows it: checked, and staged where it should be."""
    if not isinstance(f, dict) or set(f) != set(FILE_FIELDS):
        return False
    kind, name, size, sha = f["kind"], f["file_name"], f["size"], f["sha256"]
    if kind not in submission.KINDS or not isinstance(name, str) or not isinstance(sha, str):
        return False
    if type(size) is not int or type(f["generation"]) is not int or not SHA256.fullmatch(sha):
        return False
    if submission.file_problem(kind, name, size) is not None:
        return False
    return bool(
        f["content_type"] == submission.CONTENT_TYPES[Path(name).suffix.lower()]
        and f["key"] == submission.staged_key(sha, name)
    )


def _application(engine: Engine, application_id: int) -> Any:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT id, company, role FROM applications WHERE id = :a"), {"a": application_id}
        ).first()
    if found is None:
        raise HTTPException(404, "no such application")
    return found


def _recorded(engine: Engine, thread_id: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            text("SELECT id FROM submissions WHERE submission_key = :k"), {"k": thread_id}
        ).scalar_one_or_none()
    return found


def _started(engine: Engine, thread_id: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM threads WHERE thread_id = :t"), {"t": thread_id}
            ).first()
            is not None
        )
