"""Settings come from environment variables only."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jhunt.platform.config import Settings

SETTINGS_VARS = [
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "JHUNT_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "MODEL_ROUTES",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # The test run itself may have these from `uv run --env-file .env`.
    for name in SETTINGS_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def set_required(env: pytest.MonkeyPatch) -> None:
    env.setenv("POSTGRES_USER", "u")
    env.setenv("POSTGRES_PASSWORD", "s3cret-value")
    env.setenv("POSTGRES_DB", "d")
    env.setenv("POSTGRES_HOST", "db.internal")
    env.setenv("POSTGRES_PORT", "5433")
    env.setenv("JHUNT_CONFIG_DIR", "./config")


def test_reads_environment(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)

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


@pytest.mark.parametrize("missing", ["POSTGRES_HOST", "POSTGRES_PORT"])
def test_host_and_port_have_no_default(clean_env: pytest.MonkeyPatch, missing: str) -> None:
    # A default once sent these credentials to another project's Postgres on 5432.
    set_required(clean_env)
    clean_env.delenv(missing)
    with pytest.raises(ValidationError, match=missing.lower()):
        Settings()  # type: ignore[call-arg]


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


def test_config_dir_has_no_default(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    clean_env.delenv("JHUNT_CONFIG_DIR")
    with pytest.raises(ValidationError, match="jhunt_config_dir"):
        Settings()  # type: ignore[call-arg]


def test_model_settings_are_optional_and_empty_means_unset(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    s = Settings()  # type: ignore[call-arg]
    assert (s.anthropic_api_key, s.openai_api_key) == (None, None)

    clean_env.setenv("ANTHROPIC_API_KEY", "")
    clean_env.setenv("OPENAI_API_KEY", "")
    clean_env.setenv("MODEL_ROUTES", "")
    s = Settings()  # type: ignore[call-arg]
    assert (s.anthropic_api_key, s.openai_api_key, s.model_routes) == (None, None, None)


def test_api_key_is_not_printed(clean_env: pytest.MonkeyPatch) -> None:
    set_required(clean_env)
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-0123456789")
    s = Settings()  # type: ignore[call-arg]
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-fake-0123456789"
    assert "sk-ant-fake-0123456789" not in repr(s)
    assert "sk-ant-fake-0123456789" not in str(s)
