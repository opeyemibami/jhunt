# Backlog

Deferred work that has a target week. One line per item, with where it came from. Remove an item when its pull request merges.

## Week 2

- Artifacts: every records upload hard-codes Unlocked mode and 3-year retention, with tests for both. The app must never be able to choose the mode or date. (ADR 0002, residual risk; PR 2 review)
- Artifacts: the per-application delete command lists every generation (live and noncurrent), clears retention on each and deletes each, with a test. (ADR 0002, "Deleting an application's records"; PR 2 review)
- Artifacts, CI storage: set the GitHub repository variables `GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_CI_SERVICE_ACCOUNT` from `infra/gcp/setup.sh`'s output, when the first test needs storage. (PR 2; moved from PR 6, then week 1)
- Artifacts, CI storage: CI gets its own `jhunt-ci-<suffix>` bucket with no retention, so tests never need the owner's override; grant `jhunt-ci` access to it in `setup.sh`. (PR 3 review; moved from PR 6, then week 1)

## Week 3

- Application lifecycle thread (design.md build order, weeks 1 to 2), deferred: a minimal lifecycle workflow that Capture starts, waiting at an interrupt for events; it gets real work with Tracker and timers. (Week 2 plan review)
- Tool gateway: add the side-effect assertion to `tests/guarantees/test_worker_crash.py` ("side effects once"): a side effect in the interrupted step runs once across the crash and the resume. (Week 1 plan review)
- Sources move from docs/sources.md into a sources table that Scout reads, editable from a Sources page in the web app (add, pause, remove; adding checks the endpoint). docs/sources.md stays as the seed data and the record of terms checked. (docs/sources.md PR)
- WSL isn't always on. Before Scout's daily 07:00 run, decide between keeping the PC awake and moving the worker to Cloud Run early. (PR 2 review)

## Week 16

- Confirm with a real cached call whether OpenAI's input_tokens includes cache_write_tokens (ADR 0003 assumes yes). (PR 4b review)

## Week 19

- Langfuse: self-hosted with a GCS bucket, or Langfuse Cloud. (PR 2 review; design.md, Tracing and evals)
- Gateway logs only ModelCallError; log unexpected adapter exceptions too, with the same fields and no text. (PR 4a review)

## Later

- Capture: PDF render of each snapshot (design.md, Capture: "PDF render"), in the separate Playwright render job container; fills `jd_snapshots.pdf_uri`. (Week 2 plan review)
- Capture: open the application's chat session (design.md, Capture), once the chat workspace exists (weeks 9 to 10). (Week 2 plan review)
