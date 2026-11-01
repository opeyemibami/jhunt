# 0013: Recording a submission

Status: proposed in PR 3.4. Approved by the owner on 2026-11-01 before any code: the command now and a page upload later; the three tables and `record_submission` at level 1; more than one submission per application, the latest counting; answers as a text or Markdown file.

## Context

The owner submits applications outside the system (autonomy policy: "Submit an application: not allowed"), then confirms exactly what was sent. That's locked as the submission (design.md, Workspace; backlog item 3). The design's data model has:

- **Document**: id, kind (cv, cover_note, answers), version, parent_version, uri, sha256, created_by (you or draft). It never changes; an edit is a new version.
- **Submission**: id, application_id, document versions, answers, channel, submitted_at, confirmed_by_you. It never changes.

The Workspace (chat), where the design puts this step, comes later (weeks 9 to 10), and pack drafts come in 3.6. So for now the documents are the owner's own files.

## Proposal

**1. Command: `jhunt-submission <application_id> --cv FILE [--cover-note FILE] [--answers FILE] [--other FILE ...] --channel TEXT [--submitted-at WHEN]`**

- It's a command for now. An upload form on the digest page comes later, with the Workspace (owner decision). The files are on the owner's machine. After the migration to Cloud Run the worker won't be, and a browser upload needs `python-multipart`, a new dependency.
- It prints what it will lock: the application, any earlier submissions, each file's kind, name, size and sha256, the channel, and when it was submitted. Then it asks for the application id to be typed back, which is the `confirmed_by_you` step.
- **`--submitted-at`** takes a date (`2026-11-01`, read as midnight UTC) or a date and time; a time without a zone is read as UTC. It **defaults to today's date, not the current time**: the thread id below depends on it, and a same-day re-run of the same command must land on the same thread. A time in the future is refused.
- **Paths**: `~` is expanded. A Windows path (`C:\Users\me\cv.pdf`) is refused with the WSL form as a hint (`/mnt/c/Users/me/cv.pdf`) (owner, PR 3.4).
- The same file given twice is refused, and at least one file is needed.
- `--answers` takes a text or Markdown file of the application form's questions and answers, as sent. It's stored as a document of kind `answers`.
- Files may be PDF, DOCX, ODT, TXT or MD, up to 10 MB each. Anything else is refused before anything is stored.

**2. Storage (records bucket, create-only, 3-year retention, ADR 0002)**

- **Key**: `submissions/<application dedupe_key>/<sha256>/<file name>`. The same bytes give the same key, so a repeat adopts the stored object instead of writing again.
- **Gateway**: the upload goes through the tool gateway (hard rule 2) as tool **`store_submission_file`**, under a **new autonomy action `record_submission` at level 1**: internal and reversible only by the owner, then notify. The command runs the gateway in its own process.
- **Arguments**: the local path and what the owner confirmed. The tool reads the file and refuses it (`SubmissionFileChanged`) if its sha256 changed since the owner confirmed. The dedupe key, sha256 and file name are checked before they become part of an object name.
- **The thread id comes from what was submitted, not a counter** (owner, PR 3.4). It's `submission-<application_id>-<first 12 hex>` of the sha256 of `[application_id, sorted file sha256s, channel, submitted_at]`. Each file's step key is `<kind>:<sha256>`.
  - **After a crash**, the same command lands on the same thread, so the gateway returns the uploads it recorded instead of running them again.
  - **After a success**, the command finds the submission by that id and says `already recorded: submission N` without asking again.

**3. Tables (migration 0009)**. All three are append-only, with the `forbid_change` trigger:

| Table | Columns |
|---|---|
| `documents` | id, application_id, kind (`cv`, `cover_note`, `answers`, `other`), version, parent_version (null for the owner's own file), file_name, content_type, size, uri, generation, sha256, created_by (`you` now; `draft` from 3.6), created_at |
| `submissions` | id, application_id, submission_key (the thread id, unique), channel, submitted_at, confirmed_at, confirmed_by (`owner`) |
| `submission_documents` | submission_id, document_id, primary key (submission_id, document_id) |

- A document is reused when the same bytes come again for the same application and kind. Different bytes of a kind become its next version, with the last version as `parent_version`.
- `record_submission` locks the application row, so versions are numbered one submission at a time. Recording the same `submission_key` again returns what was recorded and writes nothing.

In the same transaction as the submission:
- the application's stage becomes `submitted`;
- an event `submission.confirmed` (payload: submission id, document ids, channel) is appended.

**4. More than one submission per application** is allowed: for example, a second round or a corrected upload. Each one is locked. The latest counts as current, and none replaces another.

**5. Deleting**: `jhunt-applications-delete` also removes:
- the application's submissions, documents and their links;
- each submission thread's tool calls, found by `submission-<application_id>-%`;
- every generation of the records under `submissions/<dedupe_key>/`, as it does for snapshots.

**6. The digest** shows "Captured: application N · Submitted <date> via <channel>" for a captured posting that has a submission. "Latest" means the latest `submitted_at`, then the latest recorded.

**7. Guarantee** (owner, PR 3.4 review): "A submitted document is never lost or altered", now in AGENTS.md's and design.md's tables. The mechanism is the snapshots' one:
- create-only records with per-object retention;
- no delete for the app;
- sha256 checked on read;
- append-only `documents`, `submissions` and `submission_documents`.

`tests/guarantees/test_submission_immutable.py` proves it.

## What this doesn't do

- It doesn't send, submit or upload anything to an employer. Hard rule 4 is unchanged.
- It doesn't check that the files match what the employer received. The owner's confirmation is the record.

## Consequences

- The same files, channel and day recorded again on purpose (say, a second identical submission the same day) are treated as one submission. Giving `--submitted-at` with a time tells them apart.

- A wrong file can't be fixed: it stays recorded. The owner records a new submission, and the wrong one stays in the history. Only `jhunt-applications-delete` removes it, with the whole application.
