"""A board's job list (from staging) parsed into postings (ADR 0010).

Each posting gets the same key Capture uses (board_job_key), its scope from
the location filter, and its description HTML for the fit score (PR 3.2).
"""

import html
import json
from dataclasses import dataclass
from typing import Any

from jhunt.jobs.dedupe import board_job_key
from jhunt.workflows.scout.location import classify_location
from jhunt.workflows.scout.sources import Source


@dataclass(frozen=True)
class Posting:
    dedupe_key: str
    provider: str
    board: str
    job_id: str
    url: str
    title: str
    company: str
    location: str
    remote: bool
    in_scope: bool
    scope_reason: str
    description_html: str


def parse_board(source: Source, payload: bytes) -> list[Posting]:
    jobs: list[dict[str, Any]] = json.loads(payload).get("jobs", [])
    if source.provider == "greenhouse":
        return [_greenhouse(source, job) for job in jobs]
    # Unlisted Ashby jobs are reachable only by direct link: not for Scout.
    return [_ashby(source, job) for job in jobs if job.get("isListed", True)]


def _greenhouse(source: Source, job: dict[str, Any]) -> Posting:
    location = (job.get("location") or {}).get("name") or ""
    return _posting(
        source,
        job_id=str(job["id"]),
        url=job.get("absolute_url") or "",
        title=job.get("title") or "",
        company=job.get("company_name") or source.company,
        location=location,
        remote="remote" in location.lower(),
        description_html=html.unescape(job.get("content") or ""),  # Greenhouse escapes it
    )


def _ashby(source: Source, job: dict[str, Any]) -> Posting:
    places = [job.get("location") or ""]
    places += [s.get("location") or "" for s in job.get("secondaryLocations") or []]
    return _posting(
        source,
        job_id=str(job["id"]).lower(),
        url=job.get("jobUrl") or "",
        title=job.get("title") or "",
        company=source.company,  # Ashby doesn't name the company
        location="; ".join(p for p in places if p),
        remote=bool(job.get("isRemote")) or job.get("workplaceType") == "Remote",
        description_html=job.get("descriptionHtml") or "",
    )


def _posting(source: Source, *, job_id: str, location: str, remote: bool, **fields: Any) -> Posting:
    in_scope, reason = classify_location(location, remote)
    return Posting(
        dedupe_key=board_job_key(source.provider, job_id),
        provider=source.provider,
        board=source.board,
        job_id=job_id,
        location=location,
        remote=remote,
        in_scope=in_scope,
        scope_reason=reason,
        **fields,
    )
