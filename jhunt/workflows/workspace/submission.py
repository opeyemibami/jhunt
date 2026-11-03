"""Recording a submission: what the owner sent, locked once confirmed (ADR 0013).

The owner submits outside the system, then confirms exactly what was sent
(jhunt-submission). Each file is kept as a record by the store_submission_file
tool, through the gateway; record_submission then writes the documents, the
submission and their links, moves the application to "submitted" and appends
its event, in one transaction.

A submission's thread id comes from what was submitted, not a counter, so the
same command re-run after a crash lands on the same thread: the gateway hands
back the uploads it recorded, and record_submission returns the submission it
wrote, if it got that far.

From the application page (ADR 0018), the files are checked, then staged at
uploads/<sha256>/<file name> (stage_submission_file) for the preview. The
submission run reads each staged copy at the generation staged, refuses it
if its sha256 isn't the one shown, records it, and deletes the staged copy
(delete_staged_submission_file).
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from jhunt.platform.artifacts import RecordStore, StagingStore
from jhunt.platform.gateway import Tool

ACTION = "record_submission"
KINDS = ("cv", "cover_note", "answers", "other")
MAX_FILE_BYTES = 10_000_000
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
STAGING_PREFIX = "uploads/"
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A file name as part of an object name: no folders, nothing odd.
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ ()-]{0,199}")


class SubmissionFileChanged(Exception):
    """The file's bytes differ from what the owner confirmed: nothing stored."""


@dataclass(frozen=True)
class SubmittedFile:
    kind: str
    file_name: str
    content_type: str
    size: int
    ref: dict[str, Any]  # the stored record: uri, generation, sha256


def submission_thread_id(
    application_id: int, sha256s: list[str], channel: str, submitted_at: str
) -> str:
    """submission-<application id>-<12 hex>, from what was submitted. The same
    files (in any order), channel and time give the same id (owner, PR 3.4)."""
    what = json.dumps([application_id, sorted(sha256s), channel, submitted_at])
    return f"submission-{application_id}-{hashlib.sha256(what.encode()).hexdigest()[:12]}"


def file_problem(kind: str, file_name: str, size: int) -> str | None:
    """Why this file can't be recorded, or None: the type, the size and the
    name, checked before anything is stored (owner, ADR 0018)."""
    suffix = Path(file_name).suffix.lower()
    if suffix not in CONTENT_TYPES:
        return f"{file_name}: not a PDF, DOCX, ODT, TXT or MD file"
    if kind == "answers" and suffix not in (".txt", ".md"):
        return f"{file_name}: answers must be a TXT or MD file"
    if size > MAX_FILE_BYTES:
        return f"{file_name}: {size} bytes, over the {MAX_FILE_BYTES}-byte limit"
    if not _FILE_NAME.fullmatch(file_name):
        return f"{file_name}: rename it to letters, digits, spaces and . _ ( ) - only"
    return None


def submitted_at_utc(raw: str) -> str:
    """ISO 8601 in UTC. A date alone is midnight UTC that day, so the same
    submission given again the same day gets the same thread. ValueError if
    it isn't a date or it's in the future."""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError("use 2026-11-01 or 2026-11-01T09:30") from None
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    if when > datetime.now(UTC):
        raise ValueError(f"{raw} is in the future")
    return when.isoformat()


def staged_key(sha256: str, file_name: str) -> str:
    """Where a file waits for confirmation: keyed by its content (owner)."""
    return f"{STAGING_PREFIX}{sha256}/{file_name}"


def stage_submission_file_tool(staging: StagingStore) -> Tool:
    """Stage one uploaded file for the preview, create-only, at
    uploads/<sha256>/<file name>. Its arguments are the file's bytes (base64;
    the gateway keeps only their hash) and what the page showed for them.
    A copy already there is used only if its bytes have that sha256."""

    def run(args: dict[str, Any]) -> dict[str, Any]:
        sha, name, kind = args["sha256"], args["file_name"], args["kind"]
        data = base64.b64decode(args["data"], validate=True)
        if not _SHA256.fullmatch(sha) or hashlib.sha256(data).hexdigest() != sha:
            raise SubmissionFileChanged(f"{name}: the bytes don't have sha256 {sha}")
        problem = file_problem(kind, name, len(data))
        if problem is not None:
            raise ValueError(problem)
        content_type = CONTENT_TYPES[Path(name).suffix.lower()]
        staged, found = staging.put_or_adopt(staged_key(sha, name), data, content_type)
        if staged.sha256 != sha:
            raise SubmissionFileChanged(f"{staged.uri} holds other bytes than {name}")
        return {"key": staged.key, "generation": staged.generation, "sha256": staged.sha256,
                "size": len(found)}  # fmt: skip

    return Tool("stage_submission_file", ACTION, run)


def delete_staged_submission_file_tool(staging: StagingStore) -> Tool:
    """Delete a staged copy once its submission is recorded: that exact
    generation. One already gone counts as deleted."""

    def run(args: dict[str, Any]) -> dict[str, Any]:
        if not args["key"].startswith(STAGING_PREFIX):
            raise ValueError(f"not a staged upload: {args['key']!r}")
        return {"deleted": staging.delete(args["key"], int(args["generation"]))}

    return Tool("delete_staged_submission_file", ACTION, run)


def store_submission_file_tool(
    store: RecordStore, *, staging: StagingStore | None = None, prefix: str = "submissions/"
) -> Tool:
    """Keep one submitted file as a record, create-only, at
    <prefix><application dedupe_key>/<sha256>/<file name>.

    Its arguments are where the file is and what the owner confirmed (ADR 0008):
    a local "path" (jhunt-submission), or a "staged" copy's key and generation
    (the application page, ADR 0018). The file is read here, hashed, and
    refused if its sha256 isn't the one confirmed.
    """

    def run(args: dict[str, Any]) -> dict[str, Any]:
        key, sha, name = args["dedupe_key"], args["sha256"], args["file_name"]
        # All three become part of the object name.
        if (
            not _SHA256.fullmatch(key)
            or not _SHA256.fullmatch(sha)
            or not _FILE_NAME.fullmatch(name)
        ):
            raise ValueError(f"bad store_submission_file arguments: {args!r}")
        if "staged" in args:
            data = read_staged(staging, args["staged"], sha, name)
        else:
            data = Path(args["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            where = args.get("path") or args["staged"]["key"]
            raise SubmissionFileChanged(f"{where} changed after it was confirmed")
        record, _ = store.put_or_adopt(f"{prefix}{key}/{sha}/{name}", data, args["content_type"])
        return {
            "uri": record.uri,
            "key": record.key,
            "generation": record.generation,
            "sha256": record.sha256,
        }

    return Tool("store_submission_file", ACTION, run)


def read_staged(
    staging: StagingStore | None, staged: dict[str, Any], sha256: str, file_name: str
) -> bytes:
    """A staged copy's bytes, at the generation staged. The caller hashes them."""
    if staging is None:
        raise ValueError("no staging store: this tool reads local paths only")
    if staged["key"] != staged_key(sha256, file_name):
        raise ValueError(f"not this file's staged copy: {staged['key']!r}")
    return staging.read(staged["key"], int(staged["generation"]))


def record_submission(
    engine: Engine,
    *,
    submission_key: str,
    application_id: int,
    channel: str,
    submitted_at: str,
    files: list[SubmittedFile],
) -> tuple[int, list[int]]:
    """Write the submission once. Returns (submission id, document ids, in the
    order of files). Recording the same submission_key again returns what was
    recorded and writes nothing."""
    with engine.begin() as conn:
        # One submission at a time per application: document versions are
        # numbered from what's already there.
        conn.execute(
            text("SELECT 1 FROM applications WHERE id = :id FOR UPDATE"), {"id": application_id}
        )
        found = _recorded(conn, submission_key)
        if found is not None:
            return found
        document_ids = [_document(conn, application_id, f) for f in files]
        submission_id: int = conn.execute(
            text(
                "INSERT INTO submissions (application_id, submission_key, channel, submitted_at,"
                " confirmed_by) VALUES (:a, :k, :c, :t, 'owner') RETURNING id"
            ),
            {"a": application_id, "k": submission_key, "c": channel, "t": submitted_at},
        ).scalar_one()
        for document_id in dict.fromkeys(document_ids):  # once each, in order
            conn.execute(
                text("INSERT INTO submission_documents VALUES (:s, :d)"),
                {"s": submission_id, "d": document_id},
            )
        conn.execute(
            text("UPDATE applications SET stage = 'submitted' WHERE id = :id"),
            {"id": application_id},
        )
        payload = {"submission_id": submission_id, "document_ids": document_ids, "channel": channel}
        conn.execute(
            text(
                "INSERT INTO events (application_id, type, payload, actor)"
                " VALUES (:id, 'submission.confirmed', CAST(:p AS jsonb), 'you')"
            ),
            {"id": application_id, "p": json.dumps(payload)},
        )
    return submission_id, document_ids


def _recorded(conn: Connection, submission_key: str) -> tuple[int, list[int]] | None:
    found = conn.execute(
        text("SELECT id FROM submissions WHERE submission_key = :k"), {"k": submission_key}
    ).scalar_one_or_none()
    if found is None:
        return None
    event = conn.execute(
        text(
            "SELECT payload FROM events WHERE type = 'submission.confirmed'"
            " AND (payload ->> 'submission_id')::bigint = :s"
        ),
        {"s": found},
    ).scalar_one()
    return found, list(event["document_ids"])


def _document(conn: Connection, application_id: int, f: SubmittedFile) -> int:
    """The document for this file: the same bytes of the same kind are the same
    document; different bytes are the next version, with the last as parent."""
    if f.kind not in KINDS:
        raise ValueError(f"unknown document kind {f.kind!r}")
    same: int | None = conn.execute(
        text("SELECT id FROM documents WHERE application_id = :a AND kind = :k AND sha256 = :s"),
        {"a": application_id, "k": f.kind, "s": f.ref["sha256"]},
    ).scalar_one_or_none()
    if same is not None:
        return same
    latest = conn.execute(
        text(
            "SELECT id, version FROM documents WHERE application_id = :a AND kind = :k"
            " ORDER BY version DESC LIMIT 1"
        ),
        {"a": application_id, "k": f.kind},
    ).first()
    document_id: int = conn.execute(
        text(
            "INSERT INTO documents (application_id, kind, version, parent_version, file_name,"
            " content_type, size, uri, generation, sha256, created_by)"
            " VALUES (:a, :k, :v, :p, :n, :ct, :sz, :u, :g, :s, 'you') RETURNING id"
        ),
        {
            "a": application_id,
            "k": f.kind,
            "v": 1 if latest is None else latest.version + 1,
            "p": None if latest is None else latest.id,
            "n": f.file_name,
            "ct": f.content_type,
            "sz": f.size,
            "u": f.ref["uri"],
            "g": f.ref["generation"],
            "s": f.ref["sha256"],
        },
    ).scalar_one()
    return document_id
