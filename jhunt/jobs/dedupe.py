"""The dedupe key: one key per posting, however its URL was written (design.md:
"A posting is captured once": unique dedupe key on normalised URL plus company
and title).

The rules are deliberately conservative. Two URLs that might be different jobs
must never collapse into one key: a missed duplicate costs a second application
row, a wrong merge loses a job.
"""

import hashlib
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from jhunt.jobs.boards import board_job_id

# Query parameters that only say where a click came from. Parameters that
# identify the job itself (gh_jid, id, ...) are kept.
_TRACKING = {
    "gclid",
    "fbclid",
    "msclkid",
    "gh_src",
    "lever-source",
    "lever-origin",
    "trk",
    "trackingid",
    "refid",
    "ref",
    "source",
}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_url(url: str) -> str:
    """host/path?sorted-query, without scheme, www., default port, fragment,
    trailing slash or tracking parameters. The path's case is kept."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.lower().startswith("utm_") and name.lower() not in _TRACKING
    )
    return f"{host}{path}" + (f"?{urlencode(query)}" if query else "")


def normalise_text(value: str) -> str:
    """Case-folded, NFKC-normalised, with runs of whitespace made single spaces."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def dedupe_key(url: str, company: str, title: str) -> str:
    """sha256 of the normalised URL, company and title, one per line."""
    joined = "\n".join([normalise_url(url), normalise_text(company), normalise_text(title)])
    return hashlib.sha256(joined.encode()).hexdigest()


def board_job_key(provider: str, job_id: str) -> str:
    """The key of a job on Greenhouse, Ashby or Lever: its provider and job id.

    A job id is unique within its provider (Greenhouse ids are global numbers,
    Ashby and Lever ids are UUIDs), so the board slug isn't needed, and an
    embedded link, whose board is only found by fetching the page, gets the
    same key as the direct link without any fetch (ADR 0007).
    """
    return hashlib.sha256("\n".join(["board-job", provider, job_id.lower()]).encode()).hexdigest()


def capture_key(url: str, company: str | None = None, title: str | None = None) -> str:
    """The dedupe key for capturing this link.

    Board job links (direct or embedded): from the provider and job id only,
    so however the company and title are typed, one job has one key. Any other
    link: from the URL, company and title, which are then required.
    """
    found = board_job_id(url)
    if found is not None:
        return board_job_key(*found)
    if not company or not title:
        raise ValueError(
            "company and title are required for links that aren't Greenhouse, Ashby or Lever jobs"
        )
    return dedupe_key(url, company, title)
