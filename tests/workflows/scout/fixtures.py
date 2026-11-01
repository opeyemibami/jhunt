"""Board lists for Scout's tests: what the Greenhouse and Ashby APIs return."""

import json

from jhunt.workflows.scout.sources import Source

GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
            "title": "Platform Engineer",
            "location": {"name": "Dublin, Ireland"},
            "content": "&lt;p&gt;Run the platform.&lt;/p&gt;",
        },
        {
            "id": 102,
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/102",
            "title": "Account Executive",
            "location": {"name": "San Francisco, CA"},
            "content": "&lt;p&gt;Sell.&lt;/p&gt;",
        },
    ]
}
ASHBY = {
    "jobs": [
        {
            "id": "AB12",
            "jobUrl": "https://jobs.ashbyhq.com/beta/ab12",
            "title": "ML Engineer",
            "location": "Remote - EMEA",
            "isRemote": True,
            "descriptionHtml": "<p>Train models.</p>",
        }
    ]
}
SOURCES = [
    Source("greenhouse", "acme", "Acme"),
    Source("ashby", "beta", "Beta"),
    Source("ashby", "gone", "Gone"),  # not served: the board is not found
]


def serve_boards(pages: dict[str, tuple[int, bytes]]) -> None:
    """Serve acme and beta as their APIs would; gone is a 404."""
    pages["v1/boards/acme/jobs?content=true"] = (200, json.dumps(GREENHOUSE).encode())
    pages["posting-api/job-board/beta"] = (200, json.dumps(ASHBY).encode())
