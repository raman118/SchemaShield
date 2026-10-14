import docker
import pytest
from schemashield.execution_guard import ExecutionRejected
from schemashield.sandbox import OWNER_LABEL

pytestmark = pytest.mark.integration


def assert_cleanup(runner):
    client = docker.from_env(timeout=3)
    try:
        label = f"{OWNER_LABEL}={runner.instance_id}"
        assert not client.containers.list(all=True, filters={"label": label})
        assert not client.networks.list(filters={"label": label})
    finally:
        client.close()


def test_nullable_column_real_validation_and_cleanup(real_runner):
    report = real_runner.run(["ALTER TABLE accounts ADD COLUMN nickname text"])
    assert report["status"] == "passed", report
    assert all(check["passed"] for check in report["checks"])
    assert report["environment"]["postgres_version"].startswith("17")
    assert {"table": "accounts", "column": "nickname", "type": "text"} in report["schema"]
    assert report["metrics"]["application_queries"] > 0
    assert report["metrics"]["migration_duration_ms"] > 0
    assert_cleanup(real_runner)


def test_duplicate_column_failure_cleanup(real_runner):
    report = real_runner.run(["ALTER TABLE accounts ADD COLUMN email text"])
    assert report["status"] == "failed"
    assert report["sqlstate"] == "42701"
    assert not report["checks"]
    assert_cleanup(real_runner)


def test_concurrent_index_real_catalog_validation(real_runner):
    report = real_runner.run(["CREATE INDEX CONCURRENTLY accounts_email_idx ON accounts(email)"])
    assert report["status"] == "passed", report
    assert any(
        check["name"] == "all_indexes_valid" and check["passed"] for check in report["checks"]
    )
    assert report["metrics"]["statement_timings_ms"][0] > 0
    assert_cleanup(real_runner)


def test_unsafe_sql_never_allocates_container(real_runner):
    with pytest.raises(ExecutionRejected):
        real_runner.run(["DROP TABLE accounts"])
    assert_cleanup(real_runner)


def test_cancellation_during_startup_cleans_environment(real_runner):
    report = real_runner.run(
        ["ALTER TABLE accounts ADD COLUMN nickname text"], cancelled=lambda: True
    )
    assert report["status"] == "cancelled"
    assert_cleanup(real_runner)
