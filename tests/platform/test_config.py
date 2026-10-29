"""Settings come from environment variables only."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jhunt.platform.config import Settings

POSTGRES_VARS = [
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # The test run itself may have these from `uv run --env-file .env`.
    for name in POSTGRES_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def set_required(env: pytest.MonkeyPatch) -> None:
    env.setenv("POSTGRES_USER", "u")
    env.setenv("POSTGRES_PASSWORD", "s3cret-value")
    env.setenv("POSTGRES_DB", "d")


def test_reads_environment(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    clean_env.setenv("POSTGRES_HOST", "db.internal")
    clean_env.setenv("POSTGRES_PORT", "5433")

    s = Settings()  # type: ignore[call-arg]

    url = s.database_url()
    assert (url.drivername, url.username, url.host, url.port, url.database) == (
        "postgresql+psycopg",
        "u",
        "db.internal",
        5433,
        "d",
    )
    assert s.database_url("other").database == "other"


def test_defaults_to_local_postgres(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    s = Settings()  # type: ignore[call-arg]
    assert (s.postgres_host, s.postgres_port) == ("localhost", 5432)


def test_missing_variable_fails_fast(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("POSTGRES_USER", "u")
    with pytest.raises(ValidationError, match="postgres_password"):
        Settings()  # type: ignore[call-arg]


def test_never_reads_a_dotenv_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("POSTGRES_USER=u\nPOSTGRES_PASSWORD=p\nPOSTGRES_DB=d\n")
    clean_env.chdir(tmp_path)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_password_is_not_printed(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    s = Settings()  # type: ignore[call-arg]
    assert "s3cret-value" not in repr(s)
    assert "s3cret-value" not in str(s.database_url())
