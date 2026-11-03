"""`jhunt-applications-delete <id>`: delete one application and everything it left.

Owner only: for an application that should never have been captured, or a
privacy request. In one transaction it removes the owner's labels about it
and its drafts, the edits to its drafts (ADR 0017), its pack drafts and their
claims (ADR 0016), its submissions, documents and their links (ADR
0013), its events, the application, its
snapshot row, and its threads' tool calls, runs, threads rows and checkpoints
(Capture's, each submission's and each draft's). Then it deletes the snapshot's and the
submitted files' records, every generation, as jhunt-records-delete does
(ADR 0002).

The append-only triggers (ADR 0007) would refuse these deletes. They are
switched off for exactly these tables inside the transaction and switched back
on before it commits, so a failure part-way leaves them on (the ALTERs roll
back too). That needs the table owner, which the app's database role is today;
after the role split it runs as jhunt_owner (docs/backlog.md).

It prints everything it will remove and asks for the application id to be
typed back.
"""

import argparse
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, create_engine, text

from jhunt.platform.artifacts.curator import (
    check_prefix,
    delete_all_generations,
    list_generations,
    owner_client,
)
from jhunt.platform.config import get_settings
from jhunt.workflows import capture

# The triggers that refuse deletes, by table (migrations 0005, 0006, 0009, 0012).
_GUARDS = {
    "labels": "labels_append_only",
    "draft_edits": "draft_edits_append_only",
    "draft_claims": "draft_claims_append_only",
    "pack_drafts": "pack_drafts_append_only",
    "submission_documents": "submission_documents_append_only",
    "submissions": "submissions_append_only",
    "documents": "documents_append_only",
    "events": "events_append_only",
    "applications": "applications_no_delete",
    "jd_snapshots": "jd_snapshots_append_only",
    "tool_calls": "tool_calls_append_only",
}


@dataclass(frozen=True)
class Plan:
    application_id: int
    snapshot_id: int
    threads: list[str]
    bucket: str  # where the snapshot's and the submitted files' records are
    prefixes: list[str]  # snapshots/<dedupe_key>/, and submissions/<dedupe_key>/ if any
    rows: dict[str, int]  # table -> rows that will be deleted


# Each table's rows for the plan, as (table, WHERE clause). The order is the
# delete order: what points at the application first, then the application,
# then the snapshot it points at.
_SCOPE = [
    # Labels about this application's posting (Not interested) or its drafts.
    (
        "labels",
        "target = (SELECT dedupe_key FROM applications WHERE id = :application_id)"
        " OR target LIKE ANY (SELECT 'draft:' || id || ':%' FROM pack_drafts"
        "  WHERE application_id = :application_id)",
    ),
    (
        "draft_edits",
        "draft_id IN (SELECT id FROM pack_drafts WHERE application_id = :application_id)",
    ),
    (
        "draft_claims",
        "draft_id IN (SELECT id FROM pack_drafts WHERE application_id = :application_id)",
    ),
    ("pack_drafts", "application_id = :application_id"),
    (
        "submission_documents",
        "submission_id IN (SELECT id FROM submissions WHERE application_id = :application_id)",
    ),
    ("submissions", "application_id = :application_id"),
    ("documents", "application_id = :application_id"),
    ("events", "application_id = :application_id"),
    ("applications", "id = :application_id"),
    ("jd_snapshots", "id = :snapshot_id"),
    ("tool_calls", "thread_id = ANY(:threads)"),
    ("runs", "thread_id = ANY(:threads)"),
    ("threads", "thread_id = ANY(:threads)"),
    ("checkpoints", "thread_id = ANY(:threads)"),
    ("checkpoint_blobs", "thread_id = ANY(:threads)"),
    ("checkpoint_writes", "thread_id = ANY(:threads)"),
]


def plan(engine: Engine, application_id: int) -> Plan | None:
    """What deleting this application would remove, or None if there is none."""
    with engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT a.dedupe_key, a.snapshot_id, a.thread_id, a.chat_thread_id, s.html_uri"
                " FROM applications a JOIN jd_snapshots s ON s.id = a.snapshot_id"
                " WHERE a.id = :id"
            ),
            {"id": application_id},
        ).first()
        if found is None:
            return None
        # The Capture thread, plus the lifecycle and chat threads once they exist.
        threads = [capture.thread_id_for(found.dedupe_key)]
        threads += [t for t in (found.thread_id, found.chat_thread_id) if t]
        # Each submission's thread (submission-<id>-<hash>, ADR 0013), found by
        # its tool calls: an attempt that never got recorded has those too.
        threads += conn.execute(
            text(
                "SELECT DISTINCT thread_id FROM tool_calls WHERE thread_id LIKE :pattern"
                " ORDER BY thread_id"
            ),
            {"pattern": f"submission-{application_id}-%"},
        ).scalars()
        # Each draft's Application pack thread (ADR 0016).
        threads += conn.execute(
            text("SELECT thread_id FROM pack_drafts WHERE application_id = :a ORDER BY id"),
            {"a": application_id},
        ).scalars()
        bucket, prefix = _snapshot_folder(found.html_uri)
        prefixes = [prefix]
        document_uri = conn.execute(
            text("SELECT uri FROM documents WHERE application_id = :a LIMIT 1"),
            {"a": application_id},
        ).scalar_one_or_none()
        if document_uri is not None:
            if not document_uri.startswith(f"gs://{bucket}/"):
                raise SystemExit(
                    f"jhunt-applications-delete: {document_uri} is not in gs://{bucket}"
                )
            submitted = f"submissions/{found.dedupe_key}/"
            check_prefix(submitted)  # never a whole bucket
            prefixes.append(submitted)
        params = {
            "application_id": application_id,
            "snapshot_id": found.snapshot_id,
            "threads": threads,
        }
        rows = {
            table: int(
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {where}"), params
                ).scalar_one()
            )
            for table, where in _SCOPE
        }
    return Plan(application_id, found.snapshot_id, threads, bucket, prefixes, rows)


def delete_rows(engine: Engine, plan: Plan) -> dict[str, int]:
    """Delete the plan's rows in one transaction. Returns rows deleted per table."""
    params = {
        "application_id": plan.application_id,
        "snapshot_id": plan.snapshot_id,
        "threads": plan.threads,
    }
    with engine.begin() as conn:
        _set_guards(conn, enabled=False)
        removed = {
            table: conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params).rowcount
            for table, where in _SCOPE
        }
        _set_guards(conn, enabled=True)  # back on before the commit
    return removed


def _set_guards(conn: Connection, *, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table, trigger in _GUARDS.items():
        conn.execute(text(f"ALTER TABLE {table} {action} TRIGGER {trigger}"))


def _snapshot_folder(html_uri: str) -> tuple[str, str]:
    """gs://bucket/snapshots/<key>/page.html -> (bucket, "snapshots/<key>/")."""
    if not html_uri.startswith("gs://") or "/" not in html_uri[5:]:
        raise SystemExit(f"jhunt-applications-delete: unexpected snapshot uri {html_uri!r}")
    bucket, key = html_uri[5:].split("/", 1)
    prefix = key.rsplit("/", 1)[0] + "/"
    check_prefix(prefix)  # never a whole bucket (curator.check_prefix)
    return bucket, prefix


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jhunt-applications-delete", description=__doc__.split("\n")[0]
    )
    parser.add_argument("application_id", type=int)
    args = parser.parse_args()

    settings = get_settings()
    if settings.gcp_project is None:
        raise SystemExit("jhunt-applications-delete: set GCP_PROJECT (records are deleted too)")
    engine = create_engine(settings.database_url())
    found = plan(engine, args.application_id)
    if found is None:
        raise SystemExit(f"jhunt-applications-delete: no application {args.application_id}")

    client = owner_client(settings.gcp_project)
    print(f"application {found.application_id}, threads {', '.join(found.threads)}")
    for table, count in found.rows.items():
        print(f"  {table}: {count}")
    for prefix in found.prefixes:
        print(f"  records under gs://{found.bucket}/{prefix}:")
        for gen in list_generations(found.bucket, prefix, client):
            state = "live" if gen.live else "noncurrent"
            print(f"    gs://{found.bucket}/{gen.name}#{gen.generation} ({state})")

    typed = input("Type the application id to confirm: ")
    if typed.strip() != str(found.application_id):
        raise SystemExit("jhunt-applications-delete: not confirmed; nothing deleted")

    removed = delete_rows(engine, found)
    print("deleted rows: " + ", ".join(f"{t} {n}" for t, n in removed.items()))
    # The rows are gone; now the bytes. If this fails, the rows stay deleted and
    # jhunt-records-delete <bucket> <prefix> finishes the job.
    for prefix in found.prefixes:
        deleted = delete_all_generations(found.bucket, prefix, client)
        print(f"deleted {len(deleted)} record generations under gs://{found.bucket}/{prefix}")
