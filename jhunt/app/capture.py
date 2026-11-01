"""`jhunt-capture <url> [--company C --title T] [--wait]`: capture a posting.

For Greenhouse, Ashby and Lever job links (direct or embedded) the board's own
title and company are used and --company/--title are optional; any other link
needs both (ADR 0007).

It queues a Capture run; a running jhunt-worker does the work. With --wait it
waits for the application and prints it. The same posting (by dedupe key) is
captured once: asking again says so.
"""

import argparse
import time

from sqlalchemy import Engine, create_engine, text

from jhunt.app.registry import build_registry
from jhunt.platform.config import get_settings
from jhunt.platform.runtime.threads import ThreadExists, enqueue_start
from jhunt.workflows import capture

WAIT_SECONDS = 120


def main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-capture", description=__doc__.split("\n")[0])
    parser.add_argument("url")
    # Optional for Greenhouse, Ashby and Lever job links (direct or embedded):
    # the board's own title and company are used. Required for any other link.
    parser.add_argument("--company")
    parser.add_argument("--title")
    parser.add_argument("--wait", action="store_true", help="wait for the application")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url())
    try:
        key = capture.capture_key(args.url, args.company, args.title)
    except ValueError as exc:
        raise SystemExit(f"jhunt-capture: {exc}") from None

    existing = _application(engine, key)
    if existing is not None:
        print(f"already captured: application {existing}")
        return
    thread_id = capture.thread_id_for(key)
    try:
        enqueue_start(
            engine,
            build_registry(settings, engine),
            thread_id,
            capture.WORKFLOW,
            {"url": args.url, "company": args.company, "title": args.title, "dedupe_key": key},
        )
    except ThreadExists:
        print(f"already queued: thread {thread_id}")
        return
    print(f"queued: thread {thread_id}")
    if args.wait:
        _wait(engine, key, thread_id)


def _application(engine: Engine, key: str) -> int | None:
    with engine.connect() as conn:
        found: int | None = conn.execute(
            text("SELECT id FROM applications WHERE dedupe_key = :key"), {"key": key}
        ).scalar_one_or_none()
    return found


def _wait(engine: Engine, key: str, thread_id: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        application = _application(engine, key)
        if application is not None:
            print(f"captured: application {application}")
            return
        with engine.connect() as conn:
            failed = conn.execute(
                text("SELECT error FROM runs WHERE thread_id = :t AND status = 'failed'"),
                {"t": thread_id},
            ).scalar_one_or_none()
        if failed is not None:
            raise SystemExit(f"jhunt-capture: the capture failed: {failed}")
        time.sleep(1)
    raise SystemExit("jhunt-capture: still not captured; is jhunt-worker running?")
