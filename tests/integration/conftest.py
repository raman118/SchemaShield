"""Real Docker-only integration fixtures. Absence is a disclosed acceptance blocker."""

import secrets

import docker
import pytest
from alembic.config import Config
from psycopg import sql
from schemashield.config import Settings, get_settings
from schemashield.sandbox import SandboxRunner
from sqlalchemy import URL

from alembic import command


@pytest.fixture
def real_runner():
    runner = SandboxRunner(
        Settings(
            sandbox_enabled=True,
            sandbox_timeout_seconds=90,
            api_token="real-integration-control-token-32",
        )
    )
    capability = runner.capability()
    if not capability["sandbox_available"]:
        pytest.skip("Real PostgreSQL acceptance blocked: " + capability["reason"])
    client = docker.from_env(timeout=3)
    try:
        try:
            client.images.get(runner.settings.sandbox_image)
        except docker.errors.ImageNotFound:
            pytest.skip("Real PostgreSQL acceptance blocked: reviewed image must be pulled locally")
    finally:
        client.close()
    return runner


@pytest.fixture
def real_control(real_runner, monkeypatch):
    """Separate control database in a container owned by the test harness.

    Only trusted Alembic bootstrap sees the admin URL. Application migration
    execution always creates another disposable container via SandboxRunner.
    """
    with real_runner.environment() as (_, admin, metadata):
        database = "control_" + secrets.token_hex(6)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = URL.create(
            "postgresql+psycopg",
            username=admin.info.user,
            password=admin.info.password,
            host=admin.info.host,
            port=admin.info.port,
            database=database,
        ).render_as_string(hide_password=False)
        monkeypatch.setenv("SCHEMASHIELD_DATABASE_URL", url)
        get_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
            settings = Settings(
                database_url=url,
                api_token="real-integration-control-token-32",
                sandbox_enabled=True,
            )
            yield settings, metadata
        finally:
            get_settings.cache_clear()
