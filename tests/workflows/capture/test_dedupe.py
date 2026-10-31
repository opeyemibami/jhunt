"""The dedupe key: the same posting always gets the same key (design: "A posting
is captured once": normalised URL plus company and title)."""

import pytest

from jhunt.workflows.capture.dedupe import dedupe_key, normalise_text, normalise_url


@pytest.mark.parametrize(
    "variant",
    [
        "https://boards.greenhouse.io/acme/jobs/123",
        "http://boards.greenhouse.io/acme/jobs/123",  # scheme
        "https://BOARDS.Greenhouse.io/acme/jobs/123",  # host case
        "https://www.boards.greenhouse.io/acme/jobs/123",  # leading www.
        "https://boards.greenhouse.io:443/acme/jobs/123",  # default port
        "https://boards.greenhouse.io/acme/jobs/123/",  # trailing slash
        "https://boards.greenhouse.io/acme/jobs/123#apply",  # fragment
        "https://boards.greenhouse.io/acme/jobs/123?utm_source=li&utm_medium=x",  # tracking
        "https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&gclid=1&fbclid=2",  # tracking
        "  https://boards.greenhouse.io/acme/jobs/123  ",  # surrounding space
    ],
)
def test_equivalent_urls_normalise_the_same(variant: str) -> None:
    assert normalise_url(variant) == "boards.greenhouse.io/acme/jobs/123"


def test_query_parameters_that_identify_a_job_are_kept_and_sorted() -> None:
    a = normalise_url("https://acme.example/careers?id=7&gh_jid=99&utm_campaign=x")
    b = normalise_url("https://acme.example/careers?gh_jid=99&id=7")
    assert a == b == "acme.example/careers?gh_jid=99&id=7"


def test_different_jobs_stay_different() -> None:
    assert normalise_url("https://acme.example/jobs/123") != normalise_url(
        "https://acme.example/jobs/124"
    )
    # Paths can be case-sensitive, so their case is kept.
    assert normalise_url("https://acme.example/Jobs/A") != normalise_url(
        "https://acme.example/jobs/a"
    )


def test_text_ignores_case_and_spacing() -> None:
    assert normalise_text("  Senior   Software\tEngineer ") == "senior software engineer"
    assert normalise_text("\uff21\uff23\uff2d\uff25") == "acme"  # full-width ACME (NFKC)


def test_key_combines_url_company_and_title() -> None:
    key = dedupe_key("https://acme.example/jobs/1?utm_source=x", "ACME ", "Senior  Engineer")
    assert key == dedupe_key("http://www.acme.example/jobs/1/", "acme", "senior engineer")
    assert len(key) == 64
    assert key != dedupe_key("https://acme.example/jobs/1", "acme", "staff engineer")
    assert key != dedupe_key("https://acme.example/jobs/1", "globex", "senior engineer")
