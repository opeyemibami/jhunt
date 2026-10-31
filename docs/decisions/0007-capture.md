# 0007: Capture: data model and dedupe key

Status: accepted (owner, PR 2.3). The snapshot rules were added in PR 2.5.

## Context

Capture snapshots a job description and creates the application (design.md: Workflows, Data model). Two guarantees rest on it:

- **"A captured job description is never lost or altered."** The bytes are protected by the records bucket (ADR 0002). The row that points at them must not change either (AGENTS.md hard rule 8).
- **"A posting is captured once"**: a unique dedupe key on the normalised URL plus company and title.

design.md lists the fields but not how a snapshot's parts are stored, how "never changes" is enforced, or how a URL is normalised.

## Decision

**Tables** (migration 0005):

- **`jd_snapshots`**
  - The URL, plus one row per capture: `captured_at`, and `posting_id`, null until Scout's postings table exists.
  - Each part is a record: `text_*` and `html_*`, each with its `uri`, `generation` and `sha256`. The generation pins the exact object version that reads must use.
  - `pdf_uri` stays null until the PDF render job exists (backlog).
- **`applications`**
  - The design's fields, plus a unique `dedupe_key` and a `notes` column.
  - `thread_id` (lifecycle, week 3) and `chat_thread_id` (weeks 9 to 10) start null.
- **`events`**
  - `application_id`, `type`, `payload` (jsonb), `actor` (`you`, `workflow` or `email`) and `at`.

**Enforced in the database, by triggers**, so application code can't get round them (but see "Limit" below):

- **`jd_snapshots` and `events` are append-only.** UPDATE, DELETE and TRUNCATE all raise `restrict_violation` (SQLSTATE 23001), which reaches the app as an `IntegrityError` with "refused: these rows are kept as recorded". One function, `forbid_change()`, serves every refused change. `tool_calls` (PR 2.4), `submissions` and `labels` get the same trigger when they're created.
- **`applications` is never deleted, and changes only where it stands.**
  - DELETE and TRUNCATE are refused by `forbid_change()`. Without these, an application with no events yet, so no foreign key in the way, could be deleted by any code.
  - Stage, outcome, notes and closing time may change.
  - `id`, `dedupe_key`, `snapshot_id`, `company`, `role` and `opened_at` never change.
  - Thread ids may be set once, from null, and never move after that.

**Dedupe key** (`jhunt/workflows/capture/dedupe.py`): the sha256 of three lines.

1. **The normalised URL**:
   - host in lower case, without `www.`, the scheme or a default port;
   - path with its case kept and any trailing slash removed;
   - no fragment;
   - query without tracking parameters (`utm_*`, `gclid`, `fbclid`, `msclkid`, `gh_src`, `lever-source`, `lever-origin`, `trk`, `trackingid`, `refid`, `ref`, `source`), with the remaining parameters sorted.
2. **The company**, NFKC-normalised, case-folded and with its whitespace collapsed.
3. **The title**, normalised the same way.

The rules are conservative on purpose. A missed duplicate costs a second application row. A wrong merge loses a job. Parameters that identify a job (`gh_jid`, `id`, ...) are kept.

## Snapshot rules (PR 2.5)

**The workflow** (`capture`, version 1) has one thread per posting, `capture-<dedupe_key>`, and two steps:

1. **`snapshot`** calls the gateway tool `snapshot_posting(url, dedupe_key)` once per thread (step key `page`). A re-run gets the recorded references back.
2. **`record`** writes the snapshot row, the application (stage `captured`) and a `captured` event.

**`snapshot_posting` is one tool, not two** (ADR 0008: arguments small and deterministic, results references). It fetches and stores in one call, and returns only `{html, text}`, each with its `uri`, `generation` and `sha256`.

- **Job-board links go to the board's API, never the page** (owner bug report, 2026-10-31).
  - **The bug.** `jhunt-capture` on a non-existent Greenhouse job (`jobs/4567890`) succeeded: the missing job redirected to the board's list, which returned 200 with enough text to pass the 500-character guard. Any expired job link would have been captured as the wrong page.
  - **The rule** (`jhunt/workflows/capture/boards.py`). For links on `job-boards.greenhouse.io` or `boards.greenhouse.io` (`/<board>/jobs/<id>`), `jobs.ashbyhq.com` (`/<org>/<uuid>`) and `jobs.lever.co` (`/<site>/<uuid>`), the tool parses the board and job id and asks the board's public API:

    | Board | API | What is snapshotted |
    |---|---|---|
    | Greenhouse | `boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?content=true` | `content`, which the API entity-escapes, so it's unescaped |
    | Ashby | the posting API's board list, searched for the job id | `descriptionHtml` |
    | Lever | `api.lever.co/v0/postings/{site}/{id}` | `description`, then each list as `<h3>` plus `<ul>`, then `additional` |

  - A 404, a job missing from the board's list, or an empty description raises `FetchFailed("job not found")`. The 500-character guard doesn't apply: the API answers the question directly.
  - EU-hosted Greenhouse boards (`job-boards.eu.greenhouse.io`) have no documented API host (docs/sources.md), so they take the generic path below.
- **Any other link: refuse a redirect that changes the path.** If the final URL's path differs from the requested one (ignoring a trailing slash), the tool raises `FetchFailed("redirected from <a> to <b>; the job may have closed")`. A change of scheme or host on the same path, such as http to https or `www.`, is fine.
- **Fetch.** One GET with redirects followed and a 20 s timeout. Anything but HTTP 200 raises `FetchFailed`, as does a body over 5 MB. Nothing is stored, and the gateway records nothing, so the next attempt fetches again.
- **Refuse JavaScript-rendered pages** (owner review, PR 2.5). Many job pages, Ashby especially, build their content with JavaScript, so the fetched HTML is a near-empty shell. If the fetched page has under 500 characters of extracted text, the tool raises `FetchFailed("page looks JavaScript-rendered: <n> chars of text")` before storing anything.
  - A Scout posting avoids the problem: its snapshot comes from the board API's description (planned for Scout).
- **Check before fetching** (owner review, PR 2.6). The tool first looks for an existing `page.html` under the key (`RecordStore.existing`). If there is one, it adopts it, and `page.txt` too, without fetching at all.
  - That closes the case the guard alone couldn't help: a retry after a crash between storing and the gateway recording the result, where the earlier attempt kept a good page but the page now fetches as a JavaScript shell. The good snapshot is returned, and no request is made.
  - It also saves a request on every such retry.
- **Store.** `<prefix><dedupe_key>/page.html` and `page.txt` in the records bucket, create-only, with the fixed Unlocked 3-year retention. The dedupe key must be 64 hex characters, because it becomes part of the object name.
- **Adopt, never assume** (owner rule). If an object already exists under the name (an earlier attempt, or another capture of the same posting), it is adopted, either by `RecordStore.existing` before fetching, or by `put_or_adopt` when two uploads race.
  - Its generation is read from storage.
  - Its sha256 is computed from its downloaded bytes, never from its metadata and never from the bytes just fetched, which may differ.
  - So the first stored page wins, and its hash is the one recorded.
- **Text comes from the kept HTML.** It is extracted from the HTML that was kept, not from this fetch, so the two records always describe one page. Extraction uses the stdlib HTML parser:
  - scripts, styles, `noscript`, `template` and `svg` are dropped;
  - block tags start a new line, and whitespace is collapsed;
  - bytes are read as UTF-8, with replacement characters for anything else.

**Recording.** `record_capture` first looks for the application by `dedupe_key`. If it exists, it writes nothing and returns `(id, already_captured=True)`. Otherwise it writes the snapshot row, the application and the event in one transaction. If another capture commits first, the unique `dedupe_key` rolls the whole transaction back, including the snapshot row, and the winner's id is returned. Snapshot rows can never be deleted, so a stray one would stay for good.

**Starting a capture.** `jhunt-capture <url> --company C --title T [--wait]` computes the dedupe key and queues the start run. It says "already captured" if the application exists, and "already queued" if the thread does. With `--wait` it waits for the application. The worker wires Capture to `GCS_RECORDS_BUCKET`.

**The guarantee test** (`tests/guarantees/test_capture_once.py`, PR 2.6). Two captures of one posting, run by two real worker processes, make one application, one snapshot row and one event.

- **The same thread:** the second start is refused (`ThreadExists`).
- **Two different threads:** the workers really race. In the test's worker process only, a rendezvous holds each worker just after `record_capture` finds no application, until the other has found the same. Both then insert, and the unique `dedupe_key` decides.
- **Shown failing:** with that constraint dropped, the test makes 2 applications.
- **Why the rendezvous sits there:** a rendezvous placed before the check wasn't enough. The first worker through could commit before the second checked, so the test passed even without the constraint.

## Consequences

- **Limit: the triggers don't bind the role the app uses today.** The app connects as `POSTGRES_USER`, a superuser that owns these tables. Such a role can switch the triggers off (`ALTER TABLE ... DISABLE TRIGGER`, or `SET session_replication_role = replica`). So for now the triggers stop mistakes in application code, not a deliberate bypass.
  - The fix is two roles, before migration (G1; see the backlog). `jhunt_owner` runs migrations and owns the tables. `jhunt_app` is used at runtime: it has SELECT and INSERT on the append-only tables, and UPDATE on `applications` for the changeable columns only, with no ownership and no superuser.
  - Until that split is done, this ADR's "never" holds for code paths, not for every role.
- **Deleting an application's rows** (a privacy request, or the owner tidying up) is refused by the triggers. When that's needed, it will be an owner command that runs as `jhunt_owner` and disables the triggers inside one transaction, for one application, logging what it removed. Until then nothing in the code can delete these rows.
- **The dedupe key lives in the Capture package.** Scout (week 3) needs the same key for postings, and workflows may not import each other. When Scout lands, the module moves to a shared place, to be agreed then; the rules don't change.
- **The ADR for gateway v0 (PR 2.4) is 0008.** The week 2 plan called it 0007. This ADR, 0007, takes the Capture snapshot rules in PR 2.5, including the adopt-existing-object rule.
