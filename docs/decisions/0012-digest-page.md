# 0012: The digest page

Status: proposed in PR 3.3.

## Context

The owner's priority order puts a daily digest of Scout's postings after Scout itself (backlog, item 2). The plan decided: FastAPI and uvicorn, server-rendered, bound to 127.0.0.1 only while there is no login. The owner set the layout after the first real scores (2026-11-01). Title matches come first, sorted by score, with what matched. The other in-scope postings go under a collapsed "other in-scope jobs" section. Out-of-scope postings are a count. Every posting has a Capture button.

## Decision

**`jhunt-api`** (`jhunt.app.web:main`) serves the page with uvicorn on **127.0.0.1** only, at `JHUNT_API_PORT` (default 8000). It's in the composition root, not `jhunt.api`, because the page queues Capture runs, which needs the registry, and `jhunt.api` may not import `jhunt.app` (ADR 0001). `jhunt.api.digest.create_app(engine, registry, port=...)` builds the FastAPI app. Its docs and OpenAPI routes are off.

**The page, `GET /`**, is built in Python with every value passed through `html.escape`. There's no template library, so no new dependency for it.
- **Title matches**: in-scope postings whose `fit_matches` has a `title` entry. Sorted by `fit_score`, highest first, then by title.
- **Other in-scope jobs**: the rest of the in-scope postings, in the same order, inside a closed `<details>`. Unscored postings (from Scout v1) come last.
- **Out of scope**: only a count ("N out-of-scope postings hidden"). They're never listed.
- **Each posting shows**:
  - its score and title, linked to the posting's URL;
  - its company and scope reason;
  - what matched, in the order title, include, penalty, exclude;
  - its capture state.
- **The capture state** is one of:
  - the **Capture** button;
  - "Capture queued" or "Capture running";
  - "Capture failed: <error>";
  - "Captured: application N".
- The header gives the counts and the latest Scout run.

**Capture, `POST /postings/<id>/capture`**:
- **The browser sends only the posting's id.** The server reads the posting's provider, board and job id, and builds the board's own job link with `board_job_url`. It's never a URL from the browser.
  - That link's `capture_key` is the posting's `dedupe_key`, so the posting and its application share a key. Capture reads the job from the board API (ADR 0007), as the backlog asks, even when the posting's URL is a company careers page.
  - The posting's company and title are passed as the fallbacks the owner would type. The board's own values win; Ashby doesn't name the company.
- It queues `capture-<dedupe_key>` with `enqueue_start`. If the thread already exists, it queues nothing.
- It answers 303 back to the posting on the page. A running `jhunt-worker` does the capture.
- Only in-scope postings can be captured, since only they're shown. Anything else is a 404.

**Safety, with no login**:
- **Bound to 127.0.0.1**, so no other machine can reach it.
- **Host check on every request.** The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, otherwise 400. This stops DNS rebinding: a website whose DNS name resolves to 127.0.0.1 can't read the page.
- **Origin check on the Capture POST.** `Origin` must be this page's origin, otherwise 403. A missing or `null` Origin is refused too, so another site open in the browser can't post a capture.
- **Board text is data.** Titles, companies, scope reasons, matches and errors are escaped. A posting's link is a link only if it's `http` or `https`; otherwise the title is shown as plain text.

## Consequences

- A failed capture can't be retried from the page: its thread exists. That's the same as `jhunt-capture`. A retry needs a runtime feature, like Scout's `--retry`.
- The page shows every stored posting, including ones not seen on their board since the last run, until cleanup removes them after 30 days.
- uvicorn re-raises SIGTERM after a clean shutdown, so `jhunt-api`'s exit status is SIGTERM's, not 0.
- When there is a login (Next.js and Google sign-in, later), the Host and Origin checks stay, and the page may listen beyond 127.0.0.1.
