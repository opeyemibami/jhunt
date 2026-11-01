# 0009: `jhunt.jobs`, a shared layer for job-board knowledge

Status: accepted (owner, PR 3.1 plan review, 2026-11-01).

## Context

Scout must use the same posting key as Capture (`board_job_key`) and call the same board APIs. All of that lived in `jhunt.workflows.capture`, and workflows may not import each other (ADR 0001, the independence contract). ADR 0007 deferred the question "to a shared place, to be agreed when Scout lands".

## Decision

Job-board knowledge that more than one workflow needs lives in **`jhunt/jobs`**, a layer of its own:

- `boards.py`: job links (direct and embedded), the boards' public APIs and `JobDetails`;
- `dedupe.py`: the posting keys (`board_job_key`, the URL/company/title key, `capture_key`);
- `text.py`: `html_to_text`.

The layers contract is now **app > api > workflows > jobs > platform**:

- workflows may import `jhunt.jobs`;
- `jhunt.jobs` may import only `jhunt.platform` and libraries, never a workflow. A probe module in `jhunt.jobs` that imports `jhunt.workflows.capture` breaks the contract.
- The independence contract is unchanged: workflows still may not import each other.

It sits at `jhunt/jobs`, not under `jhunt/workflows`, so it is a layer rather than an exception to the independence rule. The owner asked for that wiring.

## Consequences

- **The move was mechanical, and its own commit.** The code didn't change. Capture's tests of the moved modules moved with them to `tests/jobs/`, and only their import lines changed. Capture's public API still re-exports the keys, so `jhunt.app` and the guarantee tests didn't change.
- **Not platform.** `jhunt.platform` stays reusable infrastructure with no job-search knowledge; `jhunt.jobs` is where that knowledge goes.
- **Only one key function.** Capture and Scout share it, so the two can't disagree.
