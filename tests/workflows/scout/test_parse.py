"""Board job lists parsed into postings, keyed like Capture (board_job_key)."""

import html
import json

from jhunt.jobs.dedupe import board_job_key
from jhunt.workflows.scout.location import Scope
from jhunt.workflows.scout.parse import parse_board
from jhunt.workflows.scout.sources import Source

GREENHOUSE = {
    "jobs": [
        {
            "id": 4012345,
            "title": "Platform Engineer",
            "company_name": "Acme Inc",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4012345",
            "location": {"name": "Dublin, Ireland"},
            "content": html.escape("<p>Build things.</p>"),
        },
        {
            "id": 99,
            "title": "Remote Engineer",
            "absolute_url": "https://acme.example/careers?gh_jid=99",
            "location": {"name": "Remote - US"},
            "content": "",
        },
    ]
}
ASHBY = {
    "jobs": [
        {
            "id": "5635DD7E-A0CA-414D-B738-71500502DBA9",
            "title": "Research Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/adaption/5635dd7e-a0ca-414d-b738-71500502dba9",
            "location": "London",
            "secondaryLocations": [{"location": "Dublin"}],
            "isRemote": False,
            "workplaceType": "Hybrid",
            "isListed": True,
            "descriptionHtml": "<p>Research.</p>",
        },
        {"id": "x", "title": "Hidden", "jobUrl": "u", "location": "", "isListed": False},
        {
            "id": "0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            "title": "Solutions Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/adaption/0b1e8f0a-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            "location": "Remote",
            "workplaceType": "Remote",
            "isListed": True,
            "descriptionHtml": "<p>Sell.</p>",
        },
    ]
}


SCOPE = Scope(northern_ireland_in_scope=False)


def test_greenhouse_jobs() -> None:
    first, second = parse_board(
        Source("greenhouse", "acme", "Acme"), json.dumps(GREENHOUSE).encode(), SCOPE
    )
    assert first.dedupe_key == board_job_key("greenhouse", "4012345")
    assert (first.job_id, first.title, first.company, first.location) == (
        "4012345",
        "Platform Engineer",
        "Acme Inc",  # the board's own company name wins over the config
        "Dublin, Ireland",
    )
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/4012345"
    assert first.description_html == "<p>Build things.</p>"  # unescaped
    assert (first.in_scope, first.scope_reason) == (True, "Dublin")
    assert (second.company, second.remote, second.in_scope) == ("Acme", True, False)


def test_ashby_jobs_skip_unlisted_and_join_locations() -> None:
    research, solutions = parse_board(
        Source("ashby", "adaption", "Adaption Labs"), json.dumps(ASHBY).encode(), SCOPE
    )
    assert research.dedupe_key == board_job_key("ashby", "5635dd7e-a0ca-414d-b738-71500502dba9")
    assert research.job_id == "5635dd7e-a0ca-414d-b738-71500502dba9"  # lower-cased
    assert (research.company, research.location, research.remote) == (
        "Adaption Labs",  # Ashby doesn't name the company: the config does
        "London; Dublin",
        False,
    )
    assert (research.in_scope, research.scope_reason) == (True, "Dublin")
    assert (solutions.remote, solutions.in_scope, solutions.scope_reason) == (
        True,
        True,
        "remote (region not stated)",
    )
