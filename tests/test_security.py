import asyncio

import httpx
import pytest
from schemashield.config import Settings
from schemashield.execution_guard import ExecutionRejected, guard_sql
from schemashield.main import create_app


def test_streamed_request_deadline_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("schemashield.main.REQUEST_BODY_TIMEOUT_SECONDS", 0.01)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'bounded.db'}", allow_sqlite=True)

    async def check():
        async def slow_body():
            yield b'{"title":'
            await asyncio.sleep(0.03)
            yield b'"late"}'

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as client:
            response = await client.post("/api/migrations", content=slow_body())
            assert response.status_code == 408
            assert response.json()["code"] == "request_timeout"

    asyncio.run(check())


@pytest.mark.parametrize(
    "sql",
    [
        "COPY accounts TO PROGRAM 'whoami'",
        "CREATE EXTENSION dblink",
        "SELECT pg_sleep(99)",
        "DO $$ BEGIN PERFORM pg_sleep(99); END $$",
        "DROP TABLE accounts",
        "ALTER TABLE accounts ADD COLUMN x text DEFAULT current_user",
        "ALTER TABLE accounts ADD COLUMN x text DEFAULT pg_read_file('/etc/passwd')",
        "CREATE TABLE pg_owned (id int)",
        "CREATE TABLE private.accounts (id int)",
        "CREATE TABLE x (id custom_type)",
        "CREATE TABLE x AS SELECT 1",
        "CREATE INDEX i ON accounts(email)",
        "CREATE INDEX CONCURRENTLY i ON accounts(lower(email))",
        "ALTER TABLE accounts DISABLE TRIGGER ALL",
        "ALTER TABLE accounts ADD COLUMN x text NOT NULL",
        "BEGIN; ALTER TABLE accounts ADD COLUMN x text; COMMIT",
        "CREATE TABLE x (id int) INHERITS (accounts)",
        "CREATE TABLE IF NOT EXISTS accounts (id text)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON accounts(email)",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email text",
        "CREATE TABLE x (id bigint REFERENCES accounts(id) ON DELETE CASCADE)",
        "ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(account_id) REFERENCES accounts(id) ON UPDATE SET NULL NOT VALID",
    ],
)
def test_dangerous_sql_rejected(sql):
    with pytest.raises(ExecutionRejected):
        guard_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE new_items (id bigint PRIMARY KEY, value text)",
        "ALTER TABLE accounts ADD COLUMN name text",
        "CREATE INDEX CONCURRENTLY accounts_email_idx ON accounts(email)",
    ],
)
def test_narrow_reviewed_subset_allowed(sql):
    assert guard_sql(sql)


def test_resource_and_configuration_limits():
    with pytest.raises(ExecutionRejected):
        guard_sql("CREATE TABLE x (id int);" * 21)
    with pytest.raises(ValueError):
        Settings(sandbox_image="postgres:latest")
    with pytest.raises(ValueError):
        Settings(sandbox_timeout_seconds=999999)


def test_sandbox_requires_operator_supplied_api_token():
    with pytest.raises(ValueError, match="must be changed when sandbox execution is enabled"):
        Settings(sandbox_enabled=True)

    settings = Settings(sandbox_enabled=True, api_token="operator-supplied-test-token-32")
    assert settings.sandbox_enabled
