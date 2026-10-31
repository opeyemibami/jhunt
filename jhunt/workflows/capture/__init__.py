"""Capture: snapshot a job description to records and open the application
(design.md: Workflows; ADR 0007). Public API for the composition root.
"""

from jhunt.workflows.capture.dedupe import dedupe_key
from jhunt.workflows.capture.graph import VERSION, WORKFLOW, build, thread_id_for
from jhunt.workflows.capture.snapshot import FetchFailed, snapshot_posting_tool

__all__ = [
    "VERSION",
    "WORKFLOW",
    "FetchFailed",
    "build",
    "dedupe_key",
    "snapshot_posting_tool",
    "thread_id_for",
]
