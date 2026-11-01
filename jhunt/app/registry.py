"""Every workflow's graph versions, and the tools they use, wired in one place.

This is the only module that imports both the workflows and the platform
services they run on: the record and staging stores, the HTTP client, the
tool gateway.
"""

import httpx
from sqlalchemy import Engine

from jhunt.platform.artifacts import RecordStore, StagingStore
from jhunt.platform.config import Settings
from jhunt.platform.gateway import ToolGateway
from jhunt.platform.runtime.registry import Registry
from jhunt.workflows import capture, scout

# Identifies us to the sites we fetch job pages from.
USER_AGENT = "jhunt/0.1 (personal job search; one fetch per captured posting, one per board a day)"


def build_registry(settings: Settings, engine: Engine) -> Registry:
    if settings.gcs_records_bucket is None:
        raise SystemExit("set GCS_RECORDS_BUCKET: Capture keeps snapshots there (ADR 0002)")
    if settings.gcs_staging_bucket is None:
        raise SystemExit("set GCS_STAGING_BUCKET: Scout keeps board lists there (ADR 0010)")
    store = RecordStore(settings.gcs_records_bucket)
    staging = StagingStore(settings.gcs_staging_bucket)
    http = httpx.Client(timeout=httpx.Timeout(20.0), headers={"User-Agent": USER_AGENT})
    tools = [
        capture.snapshot_posting_tool(store, http),
        scout.fetch_board_tool(staging, http),
        scout.store_postings_tool(engine, staging),
    ]
    gateway = ToolGateway(engine, tools)

    registry = Registry()
    registry.register(capture.WORKFLOW, capture.VERSION, capture.build(engine, gateway))
    registry.register(scout.WORKFLOW, scout.VERSION, scout.build(engine, gateway))
    return registry
