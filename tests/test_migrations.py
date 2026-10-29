"""Alembic migrations run cleanly against a real, empty Postgres database."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, inspect, pool, text

REPO_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(db_url: URL | None = None) -> Config:
    cfg = Config(toml_file=REPO_ROOT / "pyproject.toml")
    if db_url is not None:
        cfg.attributes["database_url"] = db_url
    return cfg


def test_history_is_one_straight_line() -> None:
    # Branches would make "upgrade head" ambiguous.
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    for rev in script.walk_revisions():
        assert not isinstance(rev.down_revision, tuple), f"{rev.revision} is a merge"


def test_migration_files_are_named_by_revision() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    for rev in script.walk_revisions():
        assert Path(rev.path).name.startswith(f"{rev.revision}_")


def test_upgrade_to_head_then_back_to_base(scratch_db_url: URL) -> None:
    cfg = alembic_config(scratch_db_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    engine = create_engine(scratch_db_url, poolclass=pool.NullPool)
    try:
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head

        command.downgrade(cfg, "base")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 0
            # Nothing left behind but Alembic's own bookkeeping table.
            assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
