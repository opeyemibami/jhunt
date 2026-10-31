"""The Capture tables (migration 0005): what may change, and what never can."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import URL, Connection, Engine, create_engine, pool, text
from sqlalchemy.exc import IntegrityError

# The triggers raise restrict_violation (SQLSTATE 23001), an IntegrityError;
# each test also matches the message, so a unique-key error can't pass for one.
REFUSED = IntegrityError


@pytest.fixture
def engine(migrated_db_url: URL) -> Iterator[Engine]:
    eng = create_engine(migrated_db_url, poolclass=pool.NullPool)
    yield eng
    eng.dispose()


def snapshot(conn: Connection) -> int:
    return conn.execute(
        text(
            "INSERT INTO jd_snapshots (url, text_uri, text_generation, text_sha256,"
            " html_uri, html_generation, html_sha256)"
            " VALUES ('https://jobs.example/1', 'gs://b/t', 1, 'aa', 'gs://b/h', 2, 'bb')"
            " RETURNING id"
        )
    ).scalar_one()


def application(conn: Connection, snapshot_id: int, key: str = "k1") -> int:
    return conn.execute(
        text(
            "INSERT INTO applications (dedupe_key, snapshot_id, company, role, stage)"
            " VALUES (:key, :s, 'Acme', 'Engineer', 'captured') RETURNING id"
        ),
        {"key": key, "s": snapshot_id},
    ).scalar_one()


def event(conn: Connection, application_id: int) -> int:
    return conn.execute(
        text(
            "INSERT INTO events (application_id, type, actor) VALUES (:a, 'captured', 'workflow')"
            " RETURNING id"
        ),
        {"a": application_id},
    ).scalar_one()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE jd_snapshots SET url = 'https://elsewhere' WHERE id = :id",
        "UPDATE jd_snapshots SET html_sha256 = 'cc' WHERE id = :id",
        "DELETE FROM jd_snapshots WHERE id = :id",
        "TRUNCATE jd_snapshots CASCADE",
    ],
)
def test_snapshots_never_change(engine: Engine, statement: str) -> None:
    with engine.begin() as conn:
        sid = snapshot(conn)
    with (
        pytest.raises(REFUSED, match="refused: these rows are kept as recorded"),
        engine.begin() as conn,
    ):
        conn.execute(text(statement), {"id": sid})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET type = 'applied' WHERE id = :id",
        "DELETE FROM events WHERE id = :id",
        "TRUNCATE events",
    ],
)
def test_events_never_change(engine: Engine, statement: str) -> None:
    with engine.begin() as conn:
        eid = event(conn, application(conn, snapshot(conn)))
    with (
        pytest.raises(REFUSED, match="refused: these rows are kept as recorded"),
        engine.begin() as conn,
    ):
        conn.execute(text(statement), {"id": eid})


@pytest.mark.parametrize(
    "assignment",
    [
        "stage = 'applied'",
        "outcome = 'offer'",
        "notes = 'referred by Ana'",
        "closed_at = now()",
        "thread_id = 'lifecycle-1'",  # set once, when the lifecycle thread starts
        "chat_thread_id = 'chat-1'",  # set once, when the chat opens
    ],
)
def test_application_fields_that_may_change(engine: Engine, assignment: str) -> None:
    with engine.begin() as conn:
        aid = application(conn, snapshot(conn))
        conn.execute(text(f"UPDATE applications SET {assignment} WHERE id = :id"), {"id": aid})


@pytest.mark.parametrize(
    "assignment",
    [
        "company = 'Other'",
        "role = 'Manager'",
        "dedupe_key = 'k2'",
        "snapshot_id = snapshot_id + 0, opened_at = now() - interval '1 day'",
        "id = id + 1000",
    ],
)
def test_application_fields_that_never_change(engine: Engine, assignment: str) -> None:
    with engine.begin() as conn:
        aid = application(conn, snapshot(conn))
    with pytest.raises(REFUSED, match="cannot change"), engine.begin() as conn:
        conn.execute(text(f"UPDATE applications SET {assignment} WHERE id = :id"), {"id": aid})


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM applications WHERE id = :id",
        # Plain TRUNCATE is already refused by the events foreign key; CASCADE
        # would get past that, so the applications trigger must refuse it.
        "TRUNCATE applications CASCADE",
    ],
)
def test_applications_are_never_deleted(engine: Engine, statement: str) -> None:
    # No events yet, so no foreign key would stop a delete: only the trigger can.
    with engine.begin() as conn:
        aid = application(conn, snapshot(conn))
    with pytest.raises(REFUSED, match="on applications refused"), engine.begin() as conn:
        conn.execute(text(statement), {"id": aid})


def test_thread_ids_are_set_once(engine: Engine) -> None:
    with engine.begin() as conn:
        aid = application(conn, snapshot(conn))
        conn.execute(text("UPDATE applications SET thread_id = 't1' WHERE id = :id"), {"id": aid})
    with pytest.raises(REFUSED, match="cannot change"), engine.begin() as conn:
        conn.execute(text("UPDATE applications SET thread_id = 't2' WHERE id = :id"), {"id": aid})


def test_one_application_per_dedupe_key(engine: Engine) -> None:
    key = uuid.uuid4().hex
    with engine.begin() as conn:
        application(conn, snapshot(conn), key)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        application(conn, snapshot(conn), key)
