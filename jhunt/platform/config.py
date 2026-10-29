"""Settings, read only from environment variables.

No .env file is read here: locally, `uv run --env-file .env ...` (or
`export UV_ENV_FILE=.env`) puts its values into the environment; in CI and on
Cloud Run the platform sets them. Each module adds its own fields when it first
needs them.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    # env_file=None: environment variables are the only source (AGENTS.md).
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    # Postgres: the same variables docker-compose.yml uses for the db service.
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

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
