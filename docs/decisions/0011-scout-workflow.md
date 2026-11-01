# 0011: The Scout workflow, run by hand once a day

Status: proposed in PR 3.2a, merged; the fit score and `--rescore` added in PR 3.2b, with `config/targets.toml` as approved by the owner (2026-11-01).

## Context

ADR 0010 covers Scout's sources, board lists, postings and scope. This ADR covers how they run: the graph, the command that starts it, and cleanup. The owner runs Scout by hand each morning; there is no cron (backlog, plan decisions).

## Decision

**`jhunt-scout [--retry | --rescore] [--wait]`** queues one Scout run for today; a running `jhunt-worker` does the work, as for Capture.

- **One thread per day**: `scout-<day>`, with the UTC date (Ireland's date for a morning run). Asking again the same day prints `already ran today` and queues nothing. That keeps one day's lists, keys and tool calls together.
- **The thread's input fixes the day, the sources, the scope settings and the targets**, read from `sources.toml`, `scope.toml` and `targets.toml` when the command runs. A re-run after a crash reads the same boards the same way, even if the files changed in between. A bad file stops the command before anything is queued.
- **`--retry`** (owner, PR 3.2a review) starts another run for today only if today's latest run **failed**. The new thread is `scout-<day>-2`, then `-3` and so on. It refuses when today's run succeeded or is still queued or running, and when there was no run today. Without `--retry`, a failed day says `today's run failed: …; jhunt-scout --retry starts another`. A retry uses the same day, so board lists already in staging are adopted, not fetched again.
- **`--rescore`** (PR 3.2b) starts another run for today only if today's latest run **succeeded**, with the same numbering as `--retry`. It refuses after a failure (use `--retry`), while a run is queued or running, and when there was no run today. It's for postings stored before they were scored (the first run on 2026-11-01), or after `targets.toml` changes. It reads the same day's board lists from staging and fetches nothing. `--retry` and `--rescore` can't be combined.
- **`--wait`** waits up to five minutes and prints the summary: boards, failures, new, seen again, in scope, removed.

**The graph, version 1: `fetch → store → cleanup`.**

| Node | Does | Safe to re-run because |
|---|---|---|
| `fetch` | `fetch_board` through the gateway, once per board (step `<provider>:<board>`) | the gateway returns the recorded reference; staging is create-only and adopted |
| `store` | `store_postings` through the gateway, once per fetched board: stores and scores its postings | the gateway returns the recorded counts; the upsert repeats safely anyway |
| `cleanup` | deletes postings unseen for 30 days that have no application | deleting again finds nothing |

- **`store_postings` is a tool, not node code.** It reads the board's list from staging, which is a storage call (hard rule 2), so it goes through the gateway. It reads exactly the generation `fetch_board` stored and checks its sha256. It parses with the thread's scope and upserts. Its arguments are that reference plus the source and the scope setting, and its result is three counts (ADR 0008). It runs under `fetch_postings`, level 1, the autonomy row "Fetch, dedupe, score postings".
- **A board that fails doesn't fail the run.** A 404, another HTTP status, a refused or timed-out connection, or a response with no job list raises `BoardFetchFailed`. The `fetch` node lists the board in `failed` with the message, and the run goes on. Nothing is recorded for that board, so a re-run of the node tries it again.
- **Cleanup**: `last_seen_at` older than 30 days and no `applications` row with the same `dedupe_key`. A captured posting's row stays, so the digest can link it to its application.

**The fit score** (`fit.py`, `config/targets.toml` in git, approved by the owner). It's deterministic, with no model, and every posting is scored, in scope or not:

1. **Exclude**: any `[exclude]` keyword in the **title** sets the score to 0. Only the exclude match is recorded.
2. **Title**: `[title] weight` (0.6) if any phrase is in the title.
3. **Include**: plus `[include] weight` (0.08) per keyword found in the title or the description text, up to `max` (0.4).
4. **Penalty**: minus `[penalty] weight` (0.3), once, if any penalty keyword is in the **title**. The posting is lowered, not hidden.
5. **Clamp** to 0.0 to 1.0, rounded to four places.

- **Matching** ignores case and needs whole words: no letter or digit on either side, and any whitespace between a phrase's words.
  - **A trailing "s" also matches** (owner, PR 3.2b review): "agent" matches "AI Agents Engineer", "llm" matches "LLMs", "eval" matches "evals", "ml engineer" matches "ML Engineers".
  - **Not a plural, not a match**: "ml" doesn't match "HTML", "agent" doesn't match "Agentic", and "intern" doesn't match "Internal" or "International".
  - Plurals in "-es" or "-ies" aren't covered.
- **The description** is the board's HTML turned into text (`html_to_text`).
- **What matched is stored with the score** in `postings.fit_matches` (migration 0008), as `{"title": [...], "include": [...], "penalty": [...], "exclude": [...]}` with empty lists left out. The digest shows it next to the score.
- **A posting seen again is scored again**, so a change to `targets.toml` reaches it on the next run.
- `targets.toml` is validated on load:
  - exactly the four sections and their fields;
  - weights and max between 0 and 1;
  - every list non-empty, with non-empty strings.

**Graph version 2.** The targets are new in the thread's input, so new threads start on Scout v2 (hard rule 12).
- The graph's shape is unchanged, and one builder serves both versions.
- The store node passes the targets only when the input has them, so a v1 thread still runs exactly as before and stores postings unscored.

## Consequences

- If the whole run fails (database or storage down), `jhunt-scout --retry` starts a new thread for the same day. The failed thread stays as it is, with its run's error. The runtime has no way to retry a failed run in place.
- A board that fails every day keeps its postings until cleanup removes them 30 days after they were last seen.
- The worker now needs `GCS_STAGING_BUCKET` as well as `GCS_RECORDS_BUCKET`.
- The score is keyword matching, so it has the limits of keywords: other word forms ("agentic" for "agent") count only if listed. A posting's score and its matches show what happened.
- A posting no longer on any board keeps the score from its last run until cleanup removes it.
