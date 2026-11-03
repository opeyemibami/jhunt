# 0018: Recording a submission from the browser: uploads wait in staging (U5)

Status: accepted. The owner chose option B on 2026-11-03, with three conditions, and reviews the PR because it changes the gateway path.

## Context

U5 records a submission from the application page: upload the files, choose a channel and date, see a preview with each file's name, size and sha256, then confirm. The approved plan says three things:
- It uses the same `store_submission_file` tool and `record_submission` as `jhunt-submission`, with the same thread ids, limits and guarantee.
- "Record submission" updates in place with a live status (ADR 0017, decision 1). That means it queues a run that the worker carries out.

Two facts make these hard to combine:
- **`store_submission_file` reads the file from a local path** (ADR 0013), and checks its sha256 against what the owner confirmed. That suits the command, which runs where the files are. The web app gets the bytes in an upload.
- **The API and the worker are separate processes**, and after migration (gate G1) they're separate Cloud Run services with no shared disk. AGENTS.md: "Migration must be a config change."

So the uploaded bytes have to wait somewhere between the preview and the worker's run.

The options were:
- **A. A local upload folder.** Smallest, but it breaks at migration.
- **B. The staging bucket.** Works unchanged on Cloud Run, but the tool gains a second source.
- **C. Record in the request.** No live status, and it breaks at migration.

## Decision

**B: uploads wait in the staging bucket.**

1. **Checked before staging** (owner, condition 3). The preview checks every file before anything reaches storage:
   - size, at most 10 MB;
   - type: PDF, DOCX, ODT, TXT or MD; answers only TXT or MD;
   - the file name;
   - duplicates, the channel, and the date.

   If any check fails, the page lists every problem and nothing is staged. The staging tool checks size, type and name again.
2. **Keyed by content** (owner, condition 2). Each file is staged at `uploads/<sha256>/<file name>`, create-only, through the tool gateway (`stage_submission_file`, under the existing `record_submission` action, level 1, so no new autonomy row).
   - The gateway call is on the submission's own thread (`submission_thread_id`, as the command uses), at step `stage`, with the sha256 and file name as its key.
   - The file's bytes travel base64-encoded in the tool's arguments. The gateway stores only their hash.
   - If the key already holds an object, it is adopted only if its bytes have the same sha256.
3. **The preview** shows each file's kind, name, size and sha256, the channel and the date, and any earlier submissions. Confirming needs the application id typed back, as the command does.
4. **Confirm queues a `submission` run** (workflow `submission`, version 1) on that thread. Its status updates live on the application page. It has four steps:
   1. **check**: reads every staged file at the exact generation staged, hashes what it read and compares it with the sha256 shown on the preview. Any mismatch, or a missing object, fails the run before anything is written to records.
   2. **store**: `store_submission_file` for each file, given the staged object (key and generation) instead of a path. The tool reads that generation, **hashes what it read and refuses on a mismatch** (owner, condition 1), then writes the record as before. So the sha256 recorded is the one shown on the preview.
   3. **record**: `record_submission`, unchanged.
   4. **clean**: deletes each staged copy (`delete_staged_submission_file`, the exact generation) once the submission is recorded. A copy already gone counts as deleted. Abandoned previews go with staging's 30-day lifecycle rule.
5. **Storage**: `StagingStore.delete(key, generation)` is added to `jhunt.platform.artifacts`. Records still have no delete.

**Tests need a staging bucket set up like staging.** Staged copies are deleted, and the CI bucket is set up like records, where neither test identity may delete. `infra/gcp/setup.sh` adds `gs://jhunt-ci-staging-<suffix>`, set up like staging, where `jhunt-app` and `jhunt-ci` have staging's roles (create, view, delete). Tests read its name from `GCS_TEST_STAGING_BUCKET`.

**An exception to ADR 0008's small-arguments rule.** `stage_submission_file` takes the file's bytes (base64) as a tool argument. ADR 0008 says a tool's arguments are small and deterministic, never content, because content that can change between attempts would make a re-run look like a different call. This is a deliberate exception, for two reasons:
- **The bytes are fixed by their sha256.** The same sha256 is part of the step key (`<kind>:<sha256>:<file name>`). So a repeat of the step can only carry the same bytes, and different bytes are a different step.
- **The gateway stores only `args_hash`, never the arguments.** The bytes don't go into `tool_calls`.

There's no earlier copy the tool could read the bytes from: the upload exists only in the request. The exception covers this one tool; every other tool keeps the rule.

## Consequences

- **The command and the page don't mix on one thread.** If a page submission fails partway, re-running `jhunt-submission` with the same files, channel and date lands on the same thread with different `store` arguments (a path, not a staged object). The gateway refuses that as an idempotency conflict. Recording the same submission with a different date avoids it.
- **No Retry button yet.** A failed submission run shows its reason. Retrying it means queueing another start run on the same thread, which the worker continues from its checkpoint. That needs a small runtime helper, left to a later PR (backlog).
- **Two submissions with the same file can share a staged copy.** If one of them finishes and cleans up first, the other's check finds the copy gone and fails with nothing written. Previewing it again re-stages the file.
- A record stored in step 2 before a later file fails can only happen if a staged copy changes between steps 1 and 2. Nothing in the app does that.
