"""The run queue: a Postgres table that workers lease rows from (ADR 0005).

Every function here is one short transaction of explicit SQL. Times always come
from Postgres now(), never from the worker's clock, so workers on different
machines agree on when a lease has expired.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

# A lease lasts 30 s; the worker extends it every 10 s while it works (PR 1.3).
LEASE_SECONDS = 30
# After 5 claims that all ended with an expired lease (the worker died each
# time), the run is failed instead of being retried forever.
MAX_ATTEMPTS = 5
# Error text stored on a failed run is cut to this length.
ERROR_LIMIT = 2000

# Statuses a worker may close a run with. "failed" goes through fail(), which
# also records the error.
_CLOSING = {"succeeded", "interrupted"}


@dataclass(frozen=True)
class Run:
    """A claimed run. owner is this claim's fencing token."""

    id: int
    thread_id: str
    workflow: str
    version: int
    kind: str
    payload: Any
    attempts: int
    owner: uuid.UUID


def enqueue(
    engine: Engine, thread_id: str, workflow: str, version: int, kind: str, payload: Any
) -> int:
    """Add a run to the queue. Returns its id."""
    with engine.begin() as conn:
        run_id: int = conn.execute(
            text(
                "INSERT INTO runs (thread_id, workflow, version, kind, payload)"
                " VALUES (:thread_id, :workflow, :version, :kind, CAST(:payload AS jsonb))"
                " RETURNING id"
            ),
            {
                "thread_id": thread_id,
                "workflow": workflow,
                "version": version,
                "kind": kind,
                "payload": json.dumps(payload),
            },
        ).scalar_one()
    return run_id


def claim(
    engine: Engine, lease_seconds: int = LEASE_SECONDS, max_attempts: int = MAX_ATTEMPTS
) -> Run | None:
    """Lease the next runnable run, or return None if there is nothing to do."""
    owner = uuid.uuid4()
    try:
        with engine.begin() as conn:
            # 1. Fail runs that have used up their attempts. Without this, such
            #    a run stays the oldest open run on its thread, and step 2 would
            #    never let any later run on that thread go first.
            conn.execute(
                text(
                    "UPDATE runs SET status = 'failed', lease_owner = NULL, lease_until = NULL,"
                    "  error = 'gave up after ' || attempts || ' attempts: the lease expired"
                    " each time', updated_at = now()"
                    " WHERE id IN ("
                    "   SELECT id FROM runs"
                    "   WHERE status = 'running' AND lease_until < now() AND attempts >= :max"
                    "   FOR UPDATE SKIP LOCKED)"
                ),
                {"max": max_attempts},
            )

            # 2. Pick one run that is queued, or whose worker's lease expired.
            #    Only the oldest open run of each thread qualifies, so a second
            #    run on a busy thread waits its turn (one active run per thread).
            #    SKIP LOCKED: rows another worker is claiming right now are
            #    skipped, not waited on, so workers never block each other.
            run_id = conn.execute(
                text(
                    "SELECT r.id FROM runs r"
                    " WHERE (r.status = 'queued'"
                    "        OR (r.status = 'running' AND r.lease_until < now()))"
                    "   AND r.attempts < :max"
                    "   AND r.id = (SELECT min(o.id) FROM runs o"
                    "               WHERE o.thread_id = r.thread_id"
                    "                 AND o.status IN ('queued', 'running'))"
                    " ORDER BY r.id"
                    " FOR UPDATE SKIP LOCKED"
                    " LIMIT 1"
                ),
                {"max": max_attempts},
            ).scalar_one_or_none()
            if run_id is None:
                return None

            # 3. Take the lease with a new owner token.
            row = (
                conn.execute(
                    text(
                        "UPDATE runs SET status = 'running', lease_owner = :owner,"
                        "  lease_until = now() + make_interval(secs => :lease),"
                        "  attempts = attempts + 1, updated_at = now()"
                        " WHERE id = :id"
                        " RETURNING id, thread_id, workflow, version, kind, payload, attempts"
                    ),
                    {"owner": owner, "lease": lease_seconds, "id": run_id},
                )
                .mappings()
                .one()
            )
    except IntegrityError:
        # The one-running-run-per-thread index refused the claim: another worker
        # took a run on this thread at the same moment. Nothing was changed;
        # the caller simply tries again later.
        return None
    return Run(owner=owner, **row)


def extend(engine: Engine, run: Run, lease_seconds: int = LEASE_SECONDS) -> bool:
    """Push the lease out. False means this worker no longer owns the run."""
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE runs SET lease_until = now() + make_interval(secs => :lease),"
                "  updated_at = now()"
                " WHERE id = :id AND lease_owner = :owner AND status = 'running'"
            ),
            {"lease": lease_seconds, "id": run.id, "owner": run.owner},
        )
        return result.rowcount == 1


def finish(engine: Engine, run: Run, status: str) -> bool:
    """Close the run as succeeded or interrupted. False means this worker lost it."""
    if status not in _CLOSING:
        raise ValueError(f"finish() closes runs as {sorted(_CLOSING)}, not {status!r}")
    return _close(engine, run, status, None)


def fail(engine: Engine, run: Run, error: str) -> bool:
    """Close the run as failed, recording why. False means this worker lost it."""
    return _close(engine, run, "failed", error[:ERROR_LIMIT])


def _close(engine: Engine, run: Run, status: str, error: str | None) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "UPDATE runs SET status = :status, error = :error,"
                "  lease_owner = NULL, lease_until = NULL, updated_at = now()"
                " WHERE id = :id AND lease_owner = :owner AND status = 'running'"
            ),
            {"status": status, "error": error, "id": run.id, "owner": run.owner},
        )
        return result.rowcount == 1
