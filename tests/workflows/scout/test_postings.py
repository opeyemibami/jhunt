"""Postings are stored, in scope or not, and refreshed when seen again."""

from collections.abc import Iterator

import pytest
from sqlalchemy import URL, Engine, create_engine, pool, text

from jhunt.workflows.scout.parse import Posting
from jhunt.workflows.scout.postings import upsert_postings


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def posting(job_id: str, title: str, in_scope: bool, reason: str) -> Posting:
    return Posting(
        dedupe_key=job_id * 8,
        provider="greenhouse",
        board="acme",
        job_id=job_id,
        url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        title=title,
        company="Acme",
        location="Dublin" if in_scope else "London",
        remote=False,
        in_scope=in_scope,
        scope_reason=reason,
        description_html="<p>x</p>",
    )


def stored(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        return [
            dict(r) for r in conn.execute(text("SELECT * FROM postings ORDER BY job_id")).mappings()
        ]


def test_every_posting_is_stored_with_its_scope(engine: Engine) -> None:
    counts = upsert_postings(
        engine, [posting("11", "A", True, "Dublin"), posting("22", "B", False, "out: London")]
    )
    assert counts == {"new": 2, "seen_again": 0}
    rows = stored(engine)
    assert [(r["job_id"], r["in_scope"], r["scope_reason"]) for r in rows] == [
        ("11", True, "Dublin"),
        ("22", False, "out: London"),
    ]
    assert rows[0]["fit_score"] is None  # scored in PR 3.2


def test_seen_again_updates_the_posting_and_keeps_when_it_was_first_seen(engine: Engine) -> None:
    upsert_postings(engine, [posting("11", "Engineer", True, "Dublin")])
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE postings SET fit_score = 0.7,"
                " first_seen_at = now() - interval '3 days',"
                " last_seen_at = now() - interval '1 day'"
            )
        )

    counts = upsert_postings(engine, [posting("11", "Senior Engineer", True, "Dublin")])

    assert counts == {"new": 0, "seen_again": 1}
    [row] = stored(engine)
    assert row["title"] == "Senior Engineer"
    assert row["fit_score"] == pytest.approx(0.7)  # not touched by a refresh
    with engine.connect() as conn:
        ages = conn.execute(
            text(
                "SELECT now() - first_seen_at > interval '2 days',"
                " now() - last_seen_at < interval '1 minute' FROM postings"
            )
        ).one()
    assert tuple(ages) == (True, True)
