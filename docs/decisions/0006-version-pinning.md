# 0006: Version pinning and the fingerprint guard

Status: proposed in PR 2.1 (week 2); accepted when the owner merges it.

## Context

AGENTS.md hard rule 12 says a thread keeps the graph version it started on, and a registered version is never changed in place: changes are registered as a new version. design.md adds that old versions stay registered until their last thread closes. An application's lifecycle thread can live for months, across many deploys, so this has to hold mechanically, not by convention.

## Decision

**Pinning: the `threads` table** (migration 0004): `thread_id` (primary key), `workflow`, `version`, `created_at`.

- **`enqueue_start(engine, registry, thread_id, workflow, input)`** pins a new thread to the workflow's **latest registered version**. The pin and the first run are written in one transaction. Starting a thread that already exists raises `ThreadExists`; Capture (week 2) relies on that.
- **`enqueue_resume(engine, conninfo, registry, thread_id, value)`** takes no version. It reads the pin, so no caller can resume a thread on another version. It still records the interrupt id it answers (ADR 0005).
- **`queue.enqueue`** stays as the low-level insert underneath both. Workflows and the API use `jhunt.platform.runtime.threads`.

**The fingerprint guard: the `graph_versions` table**: `(workflow, version)` primary key, `fingerprint`, `first_seen_at`.

- **The fingerprint** is a sha256 of the compiled graph's node names and edges (source, target, conditional), sorted. It is built with an in-memory checkpointer, so no database is needed to inspect the graph.
- **Recording:** each worker calls `check_versions` before it claims anything. It records any registered version it hasn't seen. If a version's fingerprint differs from the recorded one, it raises `VersionChanged`, naming the version.
- **On mismatch:** `jhunt-worker` logs the error and exits non-zero, so a deploy that edited a registered graph in place doesn't run.
- **What it catches:** nodes or edges added, removed, renamed or rewired.
- **What it doesn't catch:** a change to the code *inside* a node (its function body, prompts or tools). That still depends on review: change a node's behaviour, register a new version.

## Consequences

- **The guarantee test** (`tests/guarantees/test_version_pinning.py`):
  1. 10 threads start on v1 and stop at an interrupt.
  2. v2 is deployed next to v1, and all 10 resume and finish on v1.
  3. A new thread starts on v2.

  A second test shows a worker refusing a v1 changed in place, before claiming anything.
- **Removing an old version** is safe once no open thread is pinned to it: `SELECT ... FROM threads` joined to its open runs. A tool for that comes when there is a first old version to retire.
- **A fingerprint is recorded the first time any worker sees a version**, from the code that worker runs. If the first deploy of a version is itself wrong, fix it by registering the next version, never by editing the row.
