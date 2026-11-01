"""Capture: snapshot a job description to records and open the application
(design.md: Workflows; ADR 0007). Public API for the composition root.
"""

from jhunt.jobs.dedupe import board_job_key, capture_key, dedupe_key
from jhunt.workflows.capture.graph import VERSION, WORKFLOW, build, thread_id_for
from jhunt.workflows.capture.snapshot import FetchFailed, snapshot_posting_tool

__all__ = [
    "VERSION",
    "WORKFLOW",
    "FetchFailed",
    "board_job_key",
    "build",
    "capture_key",
    "dedupe_key",
    "snapshot_posting_tool",
    "thread_id_for",
]
