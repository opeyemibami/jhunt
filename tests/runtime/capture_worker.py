"""A real worker process running Capture, for the capture-once guarantee test.

    python -m tests.runtime.capture_worker

It wires Capture the way jhunt.app does, but to the CI bucket under the test's
prefix (GCS_TEST_BUCKET, TEST_RECORD_PREFIX), and runs the real worker loop.

With RENDEZVOUS=<n>, record_capture's "does the application exist yet?" check,
when it finds none, then waits until n workers have got that same answer (a
row each in capture_rendezvous). So they all go on to insert together, and
the database's unique dedupe_key is what decides, not timing. (Waiting before
the check isn't enough: the first worker through can commit before the other
checks.) The wrap lives only in this test process; the workflow has no hook.
"""

import logging
import os
import signal
import sys
import threading
import time
from typing import Any

import httpx
from psycopg import Connection
from sqlalchemy import Engine, create_engine

from jhunt.platform.artifacts import RecordStore
from jhunt.platform.config import get_settings
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime.registry import Registry
from jhunt.platform.runtime.worker import run_worker
from jhunt.workflows import capture
from jhunt.workflows.capture import record as capture_record

RENDEZVOUS_TABLE = "capture_rendezvous"
RENDEZVOUS_DDL = f"CREATE TABLE {RENDEZVOUS_TABLE} (pid integer PRIMARY KEY)"
RENDEZVOUS_DROP = f"DROP TABLE IF EXISTS {RENDEZVOUS_TABLE}"


def conninfo() -> str:
    url = get_settings().database_url().set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def wait_for_others(expected: int) -> None:
    with Connection.connect(conninfo(), autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO {RENDEZVOUS_TABLE} (pid) VALUES (%s) ON CONFLICT DO NOTHING",
            (os.getpid(),),
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = conn.execute(f"SELECT count(*) FROM {RENDEZVOUS_TABLE}").fetchone()
            if row is not None and row[0] >= expected:
                return
            time.sleep(0.02)


def registry_for(engine: Engine, bucket: str, prefix: str) -> Registry:
    tool = capture.snapshot_posting_tool(
        RecordStore(bucket), httpx.Client(timeout=10), prefix=prefix
    )
    registry = Registry()
    registry.register(
        capture.WORKFLOW, capture.VERSION, capture.build(engine, ToolGateway(engine, [tool]))
    )
    return registry


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    expected = int(os.environ.get("RENDEZVOUS", "0"))
    if expected:
        real_check = capture_record._application_id

        def check_then_rendezvous(*args: Any, **kwargs: Any) -> int | None:
            found = real_check(*args, **kwargs)
            if found is None:  # about to insert: wait until the others are too
                wait_for_others(expected)
            return found

        capture_record._application_id = check_then_rendezvous

    engine = create_engine(get_settings().database_url())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    registry = registry_for(engine, os.environ["GCS_TEST_BUCKET"], os.environ["TEST_RECORD_PREFIX"])
    run_worker(
        engine,
        conninfo(),
        registry,
        lease_seconds=2,
        heartbeat_seconds=0.5,
        idle_seconds=0.05,
        stop=stop,
    )


if __name__ == "__main__":
    main()
