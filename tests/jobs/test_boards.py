"""board_job_url: the board's own link for a job Scout found, which Capture
reads from the board API under the same key as the posting (ADR 0012)."""

import pytest

from jhunt.jobs.boards import BoardJob, board_job, board_job_url
from jhunt.jobs.dedupe import board_job_key, capture_key

UUID = "5635dd7e-a0ca-414d-b738-71500502dba9"


@pytest.mark.parametrize(
    ("provider", "board", "job_id", "url"),
    [
        ("greenhouse", "anthropic", "4012345", "https://job-boards.greenhouse.io/anthropic/jobs/4012345"),
        ("ashby", "mistral.ai", UUID, f"https://jobs.ashbyhq.com/mistral.ai/{UUID}"),
    ],
)  # fmt: skip
def test_a_postings_board_link_reads_back_with_the_postings_key(
    provider: str, board: str, job_id: str, url: str
) -> None:
    assert board_job_url(provider, board, job_id) == url
    assert board_job(url) == BoardJob(provider, board, job_id)
    assert capture_key(url) == board_job_key(provider, job_id)


def test_other_providers_have_no_link() -> None:
    with pytest.raises(ValueError, match="no job link"):
        board_job_url("lever", "acme", UUID)
