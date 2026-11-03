# Backlog

Deferred work that has a target week. One line per item, with where it came from. Remove an item when its pull request merges.

## Priority order (owner, 2026-10-31)

For now this replaces design.md's build order: a usable job-search tool first.

1. Capture (PR 2.5) and capture-once (PR 2.6).
2. Scout, minimal: readers for the Greenhouse and Ashby sources in docs/sources.md, a location filter (Ireland or remote-EU), a simple fit score against the owner's target roles, and a daily digest page. No email sending. Capturing a Scout posting snapshots the description from the board API (Greenhouse `content`, Ashby `descriptionHtml`) plus the URL, not a page fetch. A manual `jhunt-capture` of a URL keeps the page fetch with the 500-character guard. (Owner, PR 2.5 review)
3. Submission recording: the owner confirms what was sent; it is locked as the submission.
4. Fact bank and application pack drafts: tailored CV lines and a cover note. Every claim cites fact ids, and unsupported claims are blocked (`test_fact_bank.py`).

Deferred until after these: timers, email intake, Tracker, prep, memory, evals, and the application lifecycle thread. The week sections below keep their items, but none of them comes before this list.

**Plan for items 2 to 4** (approved by the owner, 2026-10-31). PRs in order:

- **Pack page**: the latest draft for each captured application on the digest. It shows the cover note, the CV lines with their fact ids, and the blocked claims with their reasons, plus a "Draft pack" button (ADR 0016; owner: command now, page next).
- **After 3.6b, plan first, build only once the owner approves: search profiles.**
  - **Profiles** live in `config/profiles/<name>/` (`targets.toml`, `sources.toml`, `scope.toml`). There are three to start: `platform` (today's config), `frontier-early` (frontier and enterprise labs; earlier-career titles such as Research Engineer, Member of Technical Staff and AI Engineer; no Staff penalty) and `postdoc` (postdoctoral research aligned with the owner's PhD: vision, multimodal, anomaly detection, inference).
  - **Postings stay shared.** Scores are per (posting, profile), so one posting can rank in several profiles. The digest gets a tab per profile.
  - **Postdoc sources need research first.** `docs/sources-postdoc.md` will cover jobs.ac.uk, EURAXESS, Nature Careers, Academic Positions and the Irish universities: which offer an API or feed under their terms, and which are email alerts only. Docs only; no scraping. (Owner, PR 3.6a review)
- **A postdoc pack type** (a research statement and a publication-led CV), after 3.6b's pack drafting. (Owner, PR 3.6a review)

Decisions:

- **Digest page:** FastAPI (`fastapi`, `uvicorn`), server-rendered, bound to 127.0.0.1 only while there is no login. Next.js and Google sign-in come later.
- **Configuration:** sources in `config/sources.toml` (the 13 Greenhouse and Ashby boards from docs/sources.md), location settings in `config/scope.toml` (Northern Ireland in or out; owner, PR 3.1 review) and targets in `config/targets.toml`, all in git. Facts stay in the owner's local file, imported with `jhunt-facts import`, never in git.
- **Board payloads** go to the staging bucket (30-day expiry), so tool results stay references (ADR 0008). Staging read and write are added to `jhunt.platform.artifacts`.
- **Fit score** is deterministic in v1, with no model: title phrases, include keywords (capped), exclude keywords (title only, score 0) and penalty keywords (title only), from `config/targets.toml`, approved by the owner on 2026-11-01. The method is in ADR 0011.
- **Scout runs by hand:** the owner runs `jhunt-scout` each morning; no cron (WSL). The tick endpoint comes with timers.
- **A Scout posting's snapshot** comes from the board API (Greenhouse `content`, Ashby `descriptionHtml`) plus the URL, not a page fetch.
- **Drafts:** `pack.draft` routes to `anthropic:claude-sonnet-5-5`.
- **The fact-bank validator** blocks claims with no citation, an unknown fact id, or a number not found in the cited facts. Blocked claims are listed, never reworded. Its ADR states the limit: it can't check that a claim's meaning matches its fact. That needs a judge or evals, which are deferred.
- **Self-merge:** Claude may merge PRs that touch only docs, the Scout UI or the digest page, once CI is green. Runtime, gateway, storage, migrations and anything touching the fact bank wait for the owner's review.

## Week 3

- Tool gateway: add the side-effect assertion to `tests/guarantees/test_worker_crash.py` ("side effects once"): a side effect in the interrupted step runs once across the crash and the resume. (Week 1 plan review)
- Sources move from docs/sources.md into a sources table that Scout reads, editable from a Sources page in the web app (add, pause, remove; adding checks the endpoint). docs/sources.md stays as the seed data and the record of terms checked. (docs/sources.md PR)
- Profiles page: create and edit search profiles in the browser, with the targets, sources and scope of each. It replaces the Targets page item: targets are per profile. Each version is kept, and saving re-scores stored postings for that profile. (Owner, PR 3.6a review)
- Facts page, the rest: add a fact, un-retire one, and change status or verified date in the browser (U6 edits text, evidence, level and aliases, and retires; ADR 0017). Drafts may suggest a missing fact, which the owner adds. (Owner, 2026-11-01)
- WSL isn't always on. Before Scout's daily 07:00 run, decide between keeping the PC awake and moving the worker to Cloud Run early. (PR 2 review)

## Weeks 9 to 10

- Workspace: a resume whose interrupt was already answered closes as `interrupted` without applying its value (ADR 0005). Surface it to the user as "answer not applied: this question was already answered", and show which answer won. (PR 2.1 review)

## Before migration (G1)

- Database roles: the app connects as `POSTGRES_USER`, a superuser that owns the tables, so it could disable the append-only triggers (`ALTER TABLE ... DISABLE TRIGGER`, or `session_replication_role`). Split into two roles. `jhunt_owner` runs migrations and owns the tables. `jhunt_app` is used at runtime, with SELECT and INSERT on the append-only tables, plus UPDATE on `applications` for the changeable columns only (stage, outcome, notes, closed_at, thread_id, chat_thread_id), and no ownership or superuser. The owner delete command runs as `jhunt_owner`. Then remove the limit from ADR 0007. (PR 2.3 review)

## Evals (weeks 17 to 18)

- **A meaning check for claims against their facts**: a judge model, or an eval set, that flags a claim whose meaning differs from what its cited facts say. Application 6's first draft turned "was a standard part" into "made … a standard part", which the validator (citations, numbers, level words) can't detect. It should also flag a cover-note sentence that doesn't stand alone. Until then, the drafting prompts have a rule against changing a fact's actor or verb strength (ADR 0016), asked for but not checked. (Owner, review of the first real pack; ADRs 0015 and 0016)

## Week 16

- Confirm with a real cached call whether OpenAI's input_tokens includes cache_write_tokens (ADR 0003 assumes yes). (PR 4b review)

## Week 19

- Langfuse: self-hosted with a GCS bucket, or Langfuse Cloud. (PR 2 review; design.md, Tracing and evals)
- Gateway logs only ModelCallError; log unexpected adapter exceptions too, with the same fields and no text. (PR 4a review)

## Later

- Submissions: an upload form on the digest page, besides `jhunt-submission` (owner, PR 3.4). It needs `python-multipart`, which is a new dependency to ask about then, and an upload size limit on the server. It comes with the Workspace. (ADR 0013)

- Digest: retry a failed capture from the page. The capture thread exists, so the page can only show the error (ADR 0012). Same limit as `jhunt-capture`. (PR 3.3)

- Worker startup checks that every configured bucket (records, staging) exists, and fails fast. Today, looking up an object in a missing bucket finds nothing instead of raising, so a wrong bucket name shows up only later (found while testing `jhunt-scout --wait`). (Owner, PR 3.2b review)
- `jhunt-scout --wait` prints "waiting for the worker…" every 15 seconds, so a long wait never looks like a hang. (Owner, PR 3.2b review)

- Application lifecycle thread (design.md build order, weeks 1 to 2): a minimal lifecycle workflow that Capture starts, waiting at an interrupt for events; it gets real work with Tracker and timers. Deferred by the owner's priority order. (Week 2 plan review; owner, 2026-10-31)

- Capture: PDF render of each snapshot (design.md, Capture: "PDF render"), in the separate Playwright render job container; fills `jd_snapshots.pdf_uri`. (Week 2 plan review)
- Capture: open the application's chat session (design.md, Capture), once the chat workspace exists (weeks 9 to 10). (Week 2 plan review)
