"""`jhunt-worker` entry point.

The worker has to load workflow graphs, so it lives here in the composition
root, above jhunt.workflows, and not in jhunt.platform.runtime, which may not
import workflows. This module builds the registry and hands it to the loop.
"""

import logging
import signal
import threading

from sqlalchemy import create_engine

from jhunt.app.registry import build_registry
from jhunt.platform.config import get_settings
from jhunt.platform.runtime.threads import VersionChanged
from jhunt.platform.runtime.worker import run_worker

log = logging.getLogger("jhunt.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    conninfo = settings.database_url().set(drivername="postgresql")

    # SIGTERM (docker stop, Cloud Run scale-down) stops claiming new runs; a
    # run in progress finishes first. If the process is killed instead, the
    # lease expires and another worker resumes the run.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    engine = create_engine(settings.database_url(), pool_pre_ping=True)
    registry = build_registry(settings, engine)
    log.info("jhunt-worker started")
    try:
        run_worker(engine, conninfo.render_as_string(hide_password=False), registry, stop=stop)
    except VersionChanged as exc:
        # A deploy changed a registered graph in place. Refuse to run it.
        log.critical("%s", exc)
        raise SystemExit(f"jhunt-worker: {exc}") from None
    log.info("jhunt-worker stopped")
