# 0004: What LangGraph re-runs after a worker crash

Status: proposed in PR 5; accepted when the owner merges it

## Context

AGENTS.md tells every node author to assume that "LangGraph re-runs the whole interrupted step on resume", and to make each node safe to re-run. Week 0 checks that assumption against a real crash, because the tool gateway's idempotency design (weeks 3 to 4) builds on it.

## What we ran

`tests/runtime/test_durable_hello.py`, with the three-node fixture graph `tests/runtime/hello_graph.py` (`step1` → `step2` → `step3`).

- **Probe table.** Each node first inserts a row (node, process id) into a probe table that the test creates and drops. The insert uses its own autocommit connection, so the row survives the kill.
- **Settings.** The graph runs with `durability="sync"`. Checkpointer connections use `autocommit=True` and `row_factory=dict_row`, and `LANGGRAPH_STRICT_MSGPACK=true` is set.
- **Steps.**
  1. Process A runs the thread. `step2` records its start, then blocks.
  2. The test waits for `step2`'s probe row, then sends A a real `SIGKILL`. A exits with -9.
  3. Process B, a new interpreter, resumes the thread with `invoke(None, config, durability="sync")`.

**Versions:**
- langgraph 1.2.15
- langgraph-checkpoint 4.3.0
- langgraph-checkpoint-postgres 3.2.0
- langchain-core 1.7.2
- psycopg 3.3.6
- Python 3.12.3
- PostgreSQL 16.15, from the `pgvector/pgvector:0.8.7-pg16` image

## What actually ran (2026-10-29)

Probe rows, in insert order:

| node | process |
|---|---|
| step1 | A (killed) |
| step2 | A (killed while blocked inside the node) |
| step2 | B |
| step3 | B |

Checkpoint history after the kill, before the resume:

| step | trail | next |
|---|---|---|
| -1 | `[]` | `__start__` |
| 0 | `[]` | `step1` |
| 1 | `["step1"]` | `step2` |

The last checkpoint is the one written after `step1`. Nothing from the interrupted `step2` was saved.

Checkpoint history after the resume: the same three rows, followed by two more.

| step | trail | next |
|---|---|---|
| 2 | `["step1", "step2"]` | `step3` |
| 3 | `["step1", "step2", "step3"]` | (none) |

The test passed on all 10 consecutive runs.

## Decision

The assumption in AGENTS.md holds for these versions:

- Completed steps are not re-run.
- The interrupted step is re-run from its first line, in the new process.
- Any side effect a node performs before the crash point happens again on resume.

AGENTS.md stays as it is. Every node must still be safe to re-run, and side effects go through the tool gateway with idempotency keys.

**Durability matters.** We ran the same test with `durability="exit"`. No checkpoint was on disk at the kill, so the resume failed: `EmptyInputError: Received no input for __start__`. The thread's progress was lost entirely.

With `durability="async"`, this test still passed: `step2` blocked long enough for the background write to finish. So this test cannot detect the risk of `async`, which is a crash between a step finishing and its checkpoint being written. `durability="sync"` stays mandatory.

## Consequences

- `tests/runtime/test_durable_hello.py` encodes the observed behaviour. If a LangGraph upgrade changes what re-runs, the test fails. That is the point to update this ADR and AGENTS.md.
- Migration 0002 creates the checkpointer tables with `PostgresSaver.setup()`. It runs inside Alembic's `autocommit_block()`, because setup runs `CREATE INDEX CONCURRENTLY`, which Postgres refuses inside a transaction. As a result, migrations before 0002 in the same run are committed before 0002 starts.
- `tests/test_migrations.py` checks that the checkpointer schema version matches the installed package. A package upgrade that adds checkpointer migrations fails until a new Alembic migration calls `setup()` again.
- Not covered here: two nodes in one superstep, where only one has finished at the crash; LangGraph keeps pending writes for that case. Also not covered: subgraphs and interrupts. The worker crash guarantee test (`test_worker_crash.py`, week 1 onwards) covers the real worker.
