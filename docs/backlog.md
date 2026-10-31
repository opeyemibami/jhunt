# Backlog

Deferred work that has a target week. One line per item, with where it came from. Remove an item when its pull request merges.

## Priority order (owner, 2026-10-31)

For now this replaces design.md's build order: a usable job-search tool first.

1. Capture (PR 2.5) and capture-once (PR 2.6).
2. Scout, minimal: readers for the Greenhouse and Ashby sources in docs/sources.md, a location filter (Ireland or remote-EU), a simple fit score against the owner's target roles, and a daily digest page. No email sending.
3. Submission recording: the owner confirms what was sent; it is locked as the submission.
4. Fact bank and application pack drafts: tailored CV lines and a cover note. Every claim cites fact ids, and unsupported claims are blocked (`test_fact_bank.py`).

Deferred until after these: timers, email intake, Tracker, prep, memory, evals, and the application lifecycle thread. The week sections below keep their items, but none of them comes before this list.

## Week 3

- Tool gateway: add the side-effect assertion to `tests/guarantees/test_worker_crash.py` ("side effects once"): a side effect in the interrupted step runs once across the crash and the resume. (Week 1 plan review)
- Sources move from docs/sources.md into a sources table that Scout reads, editable from a Sources page in the web app (add, pause, remove; adding checks the endpoint). docs/sources.md stays as the seed data and the record of terms checked. (docs/sources.md PR)
- WSL isn't always on. Before Scout's daily 07:00 run, decide between keeping the PC awake and moving the worker to Cloud Run early. (PR 2 review)

## Weeks 9 to 10

- Workspace: a resume whose interrupt was already answered closes as `interrupted` without applying its value (ADR 0005). Surface it to the user as "answer not applied: this question was already answered", and show which answer won. (PR 2.1 review)

## Before migration (G1)

- Database roles: the app connects as `POSTGRES_USER`, a superuser that owns the tables, so it could disable the append-only triggers (`ALTER TABLE ... DISABLE TRIGGER`, or `session_replication_role`). Split into two roles. `jhunt_owner` runs migrations and owns the tables. `jhunt_app` is used at runtime, with SELECT and INSERT on the append-only tables, plus UPDATE on `applications` for the changeable columns only (stage, outcome, notes, closed_at, thread_id, chat_thread_id), and no ownership or superuser. The owner delete command runs as `jhunt_owner`. Then remove the limit from ADR 0007. (PR 2.3 review)

## Week 16

- Confirm with a real cached call whether OpenAI's input_tokens includes cache_write_tokens (ADR 0003 assumes yes). (PR 4b review)

## Week 19

- Langfuse: self-hosted with a GCS bucket, or Langfuse Cloud. (PR 2 review; design.md, Tracing and evals)
- Gateway logs only ModelCallError; log unexpected adapter exceptions too, with the same fields and no text. (PR 4a review)

## Later

- Application lifecycle thread (design.md build order, weeks 1 to 2): a minimal lifecycle workflow that Capture starts, waiting at an interrupt for events; it gets real work with Tracker and timers. Deferred by the owner's priority order. (Week 2 plan review; owner, 2026-10-31)

- Capture: PDF render of each snapshot (design.md, Capture: "PDF render"), in the separate Playwright render job container; fills `jd_snapshots.pdf_uri`. (Week 2 plan review)
- Capture: open the application's chat session (design.md, Capture), once the chat workspace exists (weeks 9 to 10). (Week 2 plan review)
