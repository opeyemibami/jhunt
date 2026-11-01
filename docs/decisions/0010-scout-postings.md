# 0010: Scout, part 1: sources, board lists, postings and scope

Status: accepted (owner, PR 3.1 review, 2026-11-01), with the US-town and on-site region rules and the Northern Ireland setting. PR 3.2 adds the workflow and the fit score.

## Context

The owner's priority order (docs/backlog.md) puts a minimal Scout second: read the Greenhouse and Ashby boards in docs/sources.md, keep their postings, filter to Ireland or remote-EU, score them against target roles, and show a daily digest. This ADR covers the first part: sources, fetching, parsing, scope and storage.

## Decision

**Sources** (`config/sources.toml`, in git; `jhunt.workflows.scout.sources`):

- the 13 boards reachable by API in docs/sources.md: 3 on Greenhouse, 10 on Ashby;
- each entry has a provider, the board's slug, and the company name shown for its postings (Ashby's API doesn't give one);
- validated on load: provider known, board a slug, company non-empty, no duplicates.

**Fetching** (`fetch_board(provider, board, day)`, gateway action `fetch_postings`, level 1):

- one GET of the board's job list: Greenhouse `/v1/boards/{board}/jobs?content=true`, Ashby `/posting-api/job-board/{board}`;
- the list is stored as it came, create-only, in the **staging** bucket at `scout/<day>/<provider>-<board>.json` (`StagingStore`: like records but with no retention; the bucket's 30-day lifecycle removes it);
- a repeat on the same day adopts the stored list without fetching;
- the result is a reference (URI, generation, sha256) and a job count, never the list (ADR 0008);
- a 404 is "board not found"; a response without a `jobs` list is refused.

`StagingStore` and `RecordStore` are separate types sharing a private base, so code that needs records (with retention) can't be handed a staging store.

**Parsing** (`parse_board`):

| | Greenhouse | Ashby |
|---|---|---|
| Job id | `id` | `id`, lower-cased |
| URL | `absolute_url` | `jobUrl` |
| Company | `company_name`, else the source's | the source's |
| Location | `location.name` | `location` plus each `secondaryLocations` entry, joined with `; ` |
| Remote | the location says "remote" | `isRemote`, or `workplaceType` is `Remote` |
| Description | `content`, unescaped | `descriptionHtml` |
| Skipped | nothing | jobs with `isListed: false` (direct-link only) |

Each posting's key is `board_job_key(provider, job_id)`, the same key Capture uses (ADR 0007, `jhunt.jobs`), so a captured posting and its application share a `dedupe_key`.

**Scope** (`classify_location(location, remote, scope) -> (in_scope, reason)`). Every posting is kept; the filter only labels it (owner review). A posting may list several locations (`London; Dublin`, `London | Dublin`); each is read on its own, and any one Irish location puts the posting in scope. In order:

1. **An Irish place** (Dublin, Cork, Galway, Limerick), "Ireland" (including "Republic of Ireland"), or the code "IE" on its own (`Remote - IE`) is in scope. The reason is the place, or "Ireland".
   - **US towns are out.** An Irish city name in a location that names the US is not Irish. "Names the US" means "United States", "USA", "U.S." or a capital "US", or a state (code or name, with an optional ZIP) after a comma. So `Dublin, CA`, `Dublin, Ohio, United States` and `Galway, NY` are out, as `out: Dublin, CA (US)` (owner review).
   - **Northern Ireland, Belfast and Derry are removed first**, since they contain "Ireland". They are in the UK, which means a different work permit.
2. **Northern Ireland** follows `northern_ireland_in_scope` in `config/scope.toml` (in git). It is `false` for now (owner decision), so these postings are out, as `out: … (UK)`. When it is `true`, they are in, as `Northern Ireland`. The owner can flip it without a code change.
3. **A region that includes Ireland** (EMEA, Europe, EU, worldwide, anywhere, global, international) is in scope **only if the posting is remote**: Ashby's remote flag, or the word "remote" in the location. The reason is `region <name>`.
   - An on-site posting that mentions a region is an office in that region, so it is out, as `out: on-site <place> (<region>)`, for example `out: on-site London, UK (EMEA)` (owner review). A region with no place is `out: on-site (EMEA)`.
4. **A bare "Remote"**, meaning the remote flag or the word "remote" with no country or region, is **in scope, flagged as `remote (region not stated)`** (owner decision, PR 3.1 review).
   - The region can't be told from the text, and hiding a job that might be open to Ireland costs more than glancing at one that isn't.
   - The digest shows the reason, so these can be read with that in mind.
5. **Anything else is out of scope**, with the location as the reason (`out: San Francisco, CA`, `out: Remote - US`, `out: Remote - Germany`), or `out: no location`.

`config/scope.toml` is validated on load: the one setting must be there, true or false, with no unknown keys.

**Storage** (migration 0007, `postings`):

- every posting, with `in_scope` and `scope_reason`;
- `fit_score` stays null until PR 3.2;
- `upsert_postings` inserts new postings and refreshes known ones (title, company, URL, location, scope, `last_seen_at`) with `INSERT ... ON CONFLICT (dedupe_key) DO UPDATE`. It keeps `first_seen_at` and `fit_score`, and reports `new` and `seen_again`.
- Postings aren't append-only (design.md: "replaced daily"). Deleting those unseen for 30 days, unless captured, comes with the workflow in PR 3.2.

## Consequences

- The digest (PR 3.3) shows in-scope postings and a count of the hidden ones.
- Scope rules are plain text matching. A location that names only a city outside Ireland and Europe-wide regions is out; one that names a European country only ("Remote - Germany") is out too, since it requires living there.
- Whether a posting is remote is decided for the whole posting, not per location: `Berlin; Remote - EMEA` is in scope.
- Rules can be wrong on a location nobody tested. The posting is still stored with its reason, so a wrong label can be seen and a test added for it.
- Capturing a Scout posting should snapshot the description from the board API, not by fetching the page (owner, PR 2.5 review). The posting's URL is a board link or an embedded link, which Capture already reads from the API.
