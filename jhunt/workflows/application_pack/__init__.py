"""Application pack: CV lines and a cover note drafted from the fact bank,
every claim checked (ADR 0016). Public API for the composition root.
"""

from jhunt.workflows.application_pack.edits import Edit, latest_edits, save_edit, start_draft
from jhunt.workflows.application_pack.graph import (
    VERSIONS,
    WORKFLOW,
    build,
    build_v2,
    thread_id_for,
)
from jhunt.workflows.application_pack.pack import (
    PackConfig,
    PackConfigError,
    PackReplyError,
    cover_note_text,
    load_pack_config,
    note_paragraphs,
    paragraphs,
)
from jhunt.workflows.application_pack.tool import draft_pack_tool, extract_requirements_tool

__all__ = [
    "VERSIONS",
    "WORKFLOW",
    "Edit",
    "PackConfig",
    "PackConfigError",
    "PackReplyError",
    "build",
    "build_v2",
    "cover_note_text",
    "draft_pack_tool",
    "extract_requirements_tool",
    "latest_edits",
    "load_pack_config",
    "note_paragraphs",
    "paragraphs",
    "save_edit",
    "start_draft",
    "thread_id_for",
]
