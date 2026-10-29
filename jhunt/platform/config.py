"""Settings, read only from environment variables.

No .env file is read here: locally, `uv run --env-file .env ...` (or
`export UV_ENV_FILE=.env`) puts its values into the environment; in CI and on
Cloud Run the platform sets them. Each module adds its own fields when it first
needs them.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    # env_file=None: environment variables are the only source (AGENTS.md).
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    # Postgres: the same variables docker-compose.yml uses for the db service.
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    # No defaults for host and port: a missing value must fail here, not
    # connect to whatever Postgres happens to be listening on localhost:5432.
    postgres_host: str
    postgres_port: int

    # Directory holding the non-secret config files from git, such as
    # model_routes.toml. Required, with no default, so the app never guesses
    # where its config is from the working directory.
    jhunt_config_dir: Path

    # Model gateway. None means unset, and an empty value counts as unset. The
    # gateway requires a provider's key only when a route uses that provider.
    anthropic_api_key: SecretStr | None = None
    # Optional JSON object of step -> "provider:model", applied over
    # model_routes.toml. For local experiments only.
    model_routes: str | None = None

    @field_validator("anthropic_api_key", "model_routes", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    def database_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL for psycopg 3. Its str() hides the password."""
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database or self.postgres_db,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read the environment once per process. Missing variables raise here."""
    return Settings()  # type: ignore[call-arg]  # fields come from the environment
