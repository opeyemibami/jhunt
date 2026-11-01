"""`jhunt-submission <application_id> --cv FILE ... --channel TEXT`: record what you sent.

You submit the application yourself, outside the system. This records exactly
what you sent: the files are kept as records (create-only, 3-year retention),
and the submission is locked (ADR 0013). It prints what it will lock and asks
for the application id to be typed back.

Files: --cv, --cover-note, --answers (TXT or MD: the form's questions and your
answers), --other (any number). PDF, DOCX, ODT, TXT or MD, up to 10 MB each.
--submitted-at is a date (default today) or a date and time.

Running the same command again records nothing new: after a crash it finishes
the same submission, and after a success it says it's already recorded.
"""

import argparse
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import Engine, Row, create_engine, text

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.config import get_settings
from jhunt.platform.gateway import ToolGateway
from jhunt.workflows.workspace.submission import (
    SubmittedFile,
    record_submission,
    store_submission_file_tool,
    submission_thread_id,
)

RECORDS_PREFIX = "submissions/"
MAX_FILE_BYTES = 10_000_000
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
_WINDOWS_PATH = re.compile(r"^([A-Za-z]):[\\/]")


@dataclass(frozen=True)
class LocalFile:
    kind: str
    path: Path
    content_type: str
    size: int
    sha256: str


def today() -> str:
    return date.today().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-submission", description=__doc__.split("\n")[0])
    parser.add_argument("application_id", type=int)
    parser.add_argument("--cv")
    parser.add_argument("--cover-note")
    parser.add_argument("--answers")
    parser.add_argument("--other", action="append", default=[])
    parser.add_argument("--channel", required=True, help="where you sent it, e.g. company site")
    parser.add_argument("--submitted-at", help="2026-11-01 or 2026-11-01T09:30 (default: today)")
    args = parser.parse_args()

    given = [("cv", args.cv), ("cover_note", args.cover_note), ("answers", args.answers)]
    given = [(kind, raw) for kind, raw in given if raw] + [("other", raw) for raw in args.other]
    if not given:
        _fail("nothing to record: give at least one file")
    files = [_local_file(kind, raw) for kind, raw in given]
    if len({f.sha256 for f in files}) < len(files):
        _fail("the same file is given twice")
    submitted_at = _submitted_at(args.submitted_at or today())

    settings = get_settings()
    if settings.gcs_records_bucket is None:
        _fail("set GCS_RECORDS_BUCKET: submitted files are kept there (ADR 0002)")
    engine = create_engine(settings.database_url())
    application = _application(engine, args.application_id)
    thread_id = submission_thread_id(
        args.application_id, [f.sha256 for f in files], args.channel, submitted_at
    )
    already = _submission_id(engine, thread_id)
    if already is not None:
        print(f"already recorded: submission {already} (application {args.application_id})")
        return

    print(f"application {args.application_id}: {application.company}, {application.role}")
    for earlier in _earlier(engine, args.application_id):
        when = f"{earlier.submitted_at:%Y-%m-%d}"
        print(f"  earlier: submission {earlier.id}, {when} via {earlier.channel}")
    for f in files:
        print(f"  {f.kind}: {f.path.name} ({f.size} bytes, sha256 {f.sha256})")
    print(f"  channel: {args.channel}")
    print(f"  submitted: {submitted_at[:16].replace('T00:00', '')}")
    print("This is locked once recorded: it can't be edited, only followed by another submission.")
    typed = input("Type the application id to confirm: ")
    if typed.strip() != str(args.application_id):
        _fail("not confirmed; nothing recorded")

    # Each file through the gateway (hard rule 2), keyed by this submission's
    # thread, its kind and its bytes: a re-run gets the recorded result back.
    gateway = ToolGateway(
        engine,
        [
            store_submission_file_tool(
                RecordStore(settings.gcs_records_bucket), prefix=RECORDS_PREFIX
            )
        ],
    )
    stored = [
        SubmittedFile(
            f.kind,
            f.path.name,
            f.content_type,
            f.size,
            gateway.call(
                thread_id,
                "store",
                f"{f.kind}:{f.sha256}",
                "store_submission_file",
                {
                    "path": str(f.path),
                    "dedupe_key": application.dedupe_key,
                    "file_name": f.path.name,
                    "content_type": f.content_type,
                    "sha256": f.sha256,
                },
                run_id=None,
            ),
        )
        for f in files
    ]
    submission_id, document_ids = record_submission(
        engine,
        submission_key=thread_id,
        application_id=args.application_id,
        channel=args.channel,
        submitted_at=submitted_at,
        files=stored,
    )
    print(
        f"recorded: submission {submission_id} (application {args.application_id},"
        f" {len(set(document_ids))} documents)"
    )


def _local_file(kind: str, raw: str) -> LocalFile:
    windows = _WINDOWS_PATH.match(raw)
    if windows:
        wsl = f"/mnt/{windows.group(1).lower()}/" + raw[3:].replace("\\", "/")
        _fail(f"{raw} looks like a Windows path; in WSL use {wsl}")
    path = Path(raw).expanduser()
    if not path.is_file():
        _fail(f"no such file: {raw}")
    content_type = CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        _fail(f"{path.name}: not a PDF, DOCX, ODT, TXT or MD file")
    if kind == "answers" and path.suffix.lower() not in (".txt", ".md"):
        _fail(f"{path.name}: answers must be a TXT or MD file")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        _fail(f"{path.name}: {size} bytes, over the {MAX_FILE_BYTES}-byte limit")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return LocalFile(kind, path.resolve(), content_type, size, sha)


def _submitted_at(raw: str) -> str:
    """ISO 8601 in UTC. A date alone is midnight UTC that day, so the same
    command re-run the same day gives the same submission."""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        _fail("--submitted-at: use 2026-11-01 or 2026-11-01T09:30")
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    if when > datetime.now(UTC):
        _fail(f"--submitted-at {raw} is in the future")
    return when.isoformat()


def _application(engine: Engine, application_id: int) -> Row[Any]:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT dedupe_key, company, role FROM applications WHERE id = :id"),
            {"id": application_id},
        ).first()
    if found is None:
        _fail(f"no application {application_id}")
    return found


def _submission_id(engine: Engine, submission_key: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            text("SELECT id FROM submissions WHERE submission_key = :k"), {"k": submission_key}
        ).scalar_one_or_none()
    return found


def _earlier(engine: Engine, application_id: int) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, submitted_at, channel FROM submissions"
                    " WHERE application_id = :a ORDER BY id"
                ),
                {"a": application_id},
            )
        )


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"jhunt-submission: {message}")
