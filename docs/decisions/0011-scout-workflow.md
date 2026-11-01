# 0011: The Scout workflow, run by hand once a day

Status: proposed in PR 3.2a. The fit score (PR 3.2b) is added to this ADR once the owner approves `config/targets.toml`.

## Context

ADR 0010 covers Scout's sources, board lists, postings and scope. This ADR covers how they run: the graph, the command that starts it, and cleanup. The owner runs Scout by hand each morning; there is no cron (backlog, plan decisions).

## Decision

**`jhunt-scout [--wait]`** queues one Scout run for today; a running `jhunt-worker` does the work, as for Capture.

- **One thread per day**: `scout-<day>`, with the UTC date (Ireland's date for a morning run). Asking again the same day prints `already ran today` and queues nothing. That keeps one day's lists, keys and tool calls together.
- **The thread's input fixes the day, the sources and the scope settings**, read from `sources.toml` and `scope.toml` when the command runs. A re-run after a crash reads the same boards the same way, even if the files changed in between. A bad file stops the command before anything is queued.
- **`--wait`** waits up to five minutes and prints the summary: boards, failures, new, seen again, in scope, removed.

**The graph, version 1: `fetch → store → cleanup`.**

| Node | Does | Safe to re-run because |
|---|---|---|
| `fetch` | `fetch_board` through the gateway, once per board (step `<provider>:<board>`) | the gateway returns the recorded reference; staging is create-only and adopted |
| `store` | `store_postings` through the gateway, once per fetched board | the gateway returns the recorded counts; the upsert repeats safely anyway |
| `cleanup` | deletes postings unseen for 30 days that have no application | deleting again finds nothing |

- **`store_postings` is a tool, not node code.** It reads the board's list from staging, which is a storage call (hard rule 2), so it goes through the gateway. It reads exactly the generation `fetch_board` stored and checks its sha256. It parses with the thread's scope and upserts. Its arguments are that reference plus the source and the scope setting, and its result is three counts (ADR 0008). It runs under `fetch_postings`, level 1, the autonomy row "Fetch, dedupe, score postings".
- **A board that fails doesn't fail the run.** A 404, another HTTP status, a refused or timed-out connection, or a response with no job list raises `BoardFetchFailed`. The `fetch` node lists the board in `failed` with the message, and the run goes on. Nothing is recorded for that board, so a re-run of the node tries it again.
- **Cleanup**: `last_seen_at` older than 30 days and no `applications` row with the same `dedupe_key`. A captured posting's row stays, so the digest can link it to its application.

## Consequences

- If the whole run fails (database or storage down), that day's thread is spent: `jhunt-scout` says the run failed, and the next day's run reads the boards again. Retrying a failed run needs a runtime feature that doesn't exist yet. Postings aren't lost, since the next day refreshes them.
- A board that fails every day keeps its postings until cleanup removes them 30 days after they were last seen.
- The worker now needs `GCS_STAGING_BUCKET` as well as `GCS_RECORDS_BUCKET`.
