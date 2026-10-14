from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEMASHIELD_", env_file=".env", extra="ignore")
    database_url: str = (
        "postgresql+psycopg://schemashield:local_control_only@localhost:5432/schemashield"
    )
    api_token: str = Field(default="local-development-token-change-me", min_length=24)
    cors_origin: str = "http://localhost:3000"
    sandbox_enabled: bool = False
    sandbox_image: str = "postgres:17.11-bookworm"
    sandbox_timeout_seconds: int = Field(default=90, ge=10, le=300)
    max_active_jobs: int = Field(default=2, ge=1, le=4)
    allow_sqlite: bool = False
    ai_gateway_url: str | None = None
    ai_gateway_token: SecretStr | None = None
    docker_host: str | None = None
    sandbox_transport: Literal["loopback", "container-network"] = "loopback"

    @model_validator(mode="after")
    def require_non_default_token_for_sandbox(self) -> "Settings":
        if self.sandbox_enabled and self.api_token == "local-development-token-change-me":
            raise ValueError(
                "SCHEMASHIELD_API_TOKEN must be changed when sandbox execution is enabled"
            )
        return self

    @field_validator("docker_host")
    @classmethod
    def local_docker_host(cls, value: str | None) -> str | None:
        if value is not None and value != "tcp://127.0.0.1:2376":
            raise ValueError("Only the local mutual TLS Docker gateway is supported")
        return value

    @field_validator("database_url")
    @classmethod
    def database_scheme(cls, value: str) -> str:
        if not value.startswith(("postgresql+psycopg://", "sqlite://")):
            raise ValueError(
                "Only PostgreSQL psycopg or explicitly enabled test SQLite is supported"
            )
        return value

    @field_validator("sandbox_image")
    @classmethod
    def pinned_image(cls, value: str) -> str:
        if value != "postgres:17.11-bookworm":
            raise ValueError("Only the reviewed pinned PostgreSQL image is permitted")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
