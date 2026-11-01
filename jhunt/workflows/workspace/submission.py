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
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.gateway import Tool

ACTION = "record_submission"
KINDS = ("cv", "cover_note", "answers", "other")
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


def store_submission_file_tool(store: RecordStore, *, prefix: str = "submissions/") -> Tool:
    """Keep one submitted file as a record, create-only, at
    <prefix><application dedupe_key>/<sha256>/<file name>.

    Its arguments are the local path and what the owner confirmed (ADR 0008):
    the file is read here and refused if its sha256 changed since.
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
        data = Path(args["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise SubmissionFileChanged(f"{args['path']} changed after it was confirmed")
        record, _ = store.put_or_adopt(f"{prefix}{key}/{sha}/{name}", data, args["content_type"])
        return {
            "uri": record.uri,
            "key": record.key,
            "generation": record.generation,
            "sha256": record.sha256,
        }

    return Tool("store_submission_file", ACTION, run)


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
