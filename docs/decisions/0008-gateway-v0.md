# 0008: Tool gateway v0

Status: proposed in PR 2.4 (week 2); accepted when the owner merges it.

## Context

AGENTS.md hard rule 2 says no side effect happens outside the tool gateway. Hard rule 3 says every tool call carries an idempotency key built from `thread_id`, node name and a stable step key. design.md adds that the gateway records each result, so a repeated call after a crash returns the recorded result instead of acting again.

The gateway is scheduled for weeks 3 to 4, but Capture (week 2) already fetches pages and writes records. The week 2 plan review therefore approved a v0 now: idempotency, recorded results, an audit row and level checks. Approvals, notifications and MCP come later.

## Decision

**`ToolGateway(engine, tools).call(thread_id, node, step_key, tool_name, args, *, run_id)`**, in order:

1. **Key.** `idempotency_key` is the sha256 of the JSON list `[thread_id, node, step_key]`. All three must be non-empty. There is never a timestamp or a random value in it. Encoding as a JSON list means `("a:b", "c")` and `("a", "b:c")` can't collide.
2. **Recorded result.** If `tool_calls` already has the key, return its `result` without running anything. If the recorded `args_hash` differs, raise `IdempotencyConflict`: the same step must always make the same call.
3. **Policy.** A tool that isn't registered is refused (`ToolRefused`). A "not allowed" action has no tool at all (AGENTS.md). The action's level is the **latest row** for it in `autonomy_settings`, where history is kept (hard rule 10). No row means the call is refused. v0 runs **level 1 only**; anything else is refused before the tool runs.
4. **Run, then record.**
   - The result is recorded with `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, then read back.
   - If two workers run the same step at once (ADR 0005, known limit), both run the tool. The first insert wins, the second does nothing, and **both return the winner's recorded result**. Neither raises.
   - So a tool may run at most twice for one key, and its result is recorded exactly once.

**Tables** (migration 0006). Both are append-only, through `forbid_change()` (hard rule 8).

| Table | Columns |
|---|---|
| `tool_calls` | `idempotency_key` (unique), `thread_id`, `node`, `step_key`, `run_id`, `tool`, `args_hash`, `level`, `approved_by` (null in v0), `outcome` (`succeeded`), `result` (jsonb), `created_at` |
| `autonomy_settings` | `action`, `level` (0, 1 or 2), `changed_at`, `changed_by`. Seeded with `fetch_postings = 1` and `snapshot_job_description = 1` (AGENTS.md, autonomy policy) |

**What v0 does not record.**

- **A tool that raises** records nothing, and the error propagates. The next attempt runs it again. A transient failure, such as a network timeout, must not become a permanent recorded answer.
- **Unknown outcomes** (design.md: "marked unknown and asks you") come with level 2 actions, where they matter.

**Arguments are small and deterministic; results are references** (owner review, PR 2.4).

- **A tool's arguments** must be small and the same every time a given step runs: URLs, ids, keys. They must never be content fetched earlier. The arguments are hashed into `args_hash` and checked on every repeat, so content that can change between attempts (such as a page fetched twice) would make a re-run look like a different call.
- **A tool's result** carries references (URIs, generations, hashes, ids), not payloads. A recorded result is returned on every repeat and lives in the audit log for good.
- **What follows.** Work that fetches something and then stores it is one tool, not two passing content between them. Capture's `snapshot_posting(url, dedupe_key)` fetches the page and stores it, and returns only references (ADR 0007).

## Consequences

- **The window a recorded result can't cover.** A crash after a tool ran but before its row was recorded means the tool runs again on resume. So every tool must be safe to repeat. The week 2 tools are, by construction (ADR 0007, PR 2.5):
  - **fetching** is read-only;
  - **the snapshot upload** is create-only, under names derived from the posting's `dedupe_key`. A repeat that finds an object adopts it: its generation is read from storage, and its sha256 is computed from the downloaded bytes, never from metadata. Fetching and storing are one tool, `snapshot_posting` (see below).
- **Level 1 says "then notify".** v0 records the call in the audit log, but sends no notification. Notifications come with the digest (weeks 3 to 4).
- **`run_id`** is passed by the caller. The worker puts it into the graph config (`config["configurable"]["run_id"]`, from PR 2.5), so nodes pass it on.
- **The gateway is in `jhunt.platform.gateway`**, one of the four packages the owner must be able to explain line by line. It is a single module of explicit SQL, with no framework.
