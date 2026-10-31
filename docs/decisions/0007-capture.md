# 0007: Capture: data model and dedupe key

Status: proposed in PR 2.3 (week 2); accepted when the owner merges it. PR 2.5 adds the snapshot rules.

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

**Enforced in the database, by triggers**, so no code path can get round them:

- **`jd_snapshots` and `events` are append-only.** UPDATE, DELETE and TRUNCATE all raise `restrict_violation` (SQLSTATE 23001), which reaches the app as an `IntegrityError` with "refused: rows are append-only". One function, `forbid_change()`, serves every append-only table. `tool_calls` (PR 2.4), `submissions` and `labels` get the same trigger when they're created.
- **`applications` changes only where it stands.**
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

## Consequences

- **Deleting an application's rows** (a privacy request, or the owner tidying up) is refused by the triggers for every role the app uses. When that's needed, it will be an owner command that disables the triggers inside one transaction, for one application, and logs what it removed. Until then nothing can delete these rows.
- **The dedupe key lives in the Capture package.** Scout (week 3) needs the same key for postings, and workflows may not import each other. When Scout lands, the module moves to a shared place, to be agreed then; the rules don't change.
- **The ADR for gateway v0 (PR 2.4) is 0008.** The week 2 plan called it 0007. This ADR, 0007, takes the Capture snapshot rules in PR 2.5, including the adopt-existing-object rule.
