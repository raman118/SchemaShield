"""Approval state-machine unit tests. Runner evidence is explicitly mocked.

These tests do not satisfy real PostgreSQL validation acceptance.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from schemashield.config import Settings
from schemashield.main import create_app
from schemashield.store import Job, Migration, new_id

TOKEN = "approval-unit-test-token-32-characters"


class MockUnitRunner:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def capability(self):
        return {
            "sandbox_enabled": True,
            "sandbox_available": True,
            "reason": "UNIT MOCK; no actual PostgreSQL",
        }

    def run(self, statements, cancelled):
        self.calls.append(statements)
        self.started.set()
        if self.block:
            assert self.release.wait(5), "Unit test failed to release mock worker"
        return {
            "status": "cancelled" if cancelled() else "passed",
            "checks": [
                {"name": "fixture_row_count_and_original_values", "passed": True},
                {"name": "all_indexes_valid", "passed": True},
                {"name": "controlled_application_queries_succeeded", "passed": True},
            ],
            "metrics": {},
            "environment": {"unit_mock": True},
            "limitations": ["Unit mock; not runtime validation evidence"],
        }


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'unit-control.db'}", allow_sqlite=True, api_token=TOKEN
    )
    runner = MockUnitRunner()
    application = create_app(settings, runner)
    with TestClient(application) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield client, application, runner


def submit(client, sql="ALTER TABLE accounts ADD COLUMN nickname text"):
    response = client.post("/api/migrations", json={"title": "Approval test", "sql": sql})
    assert response.status_code == 201
    return response.json()


def validate(client, migration, target="migration"):
    response = client.post(f"/api/migrations/{migration['id']}/validate", json={"target": target})
    assert response.status_code == 202, response.text
    return client.get(f"/api/jobs/{response.json()['id']}").json()


def approve(client, migration, evidence, **overrides):
    payload = {
        "artifact_hash": evidence["artifact_hash"],
        "report_id": evidence["id"],
        "reviewer": "unit-reviewer",
    }
    payload.update(overrides)
    return client.post(f"/api/migrations/{migration['id']}/approve", json=payload)


def test_missing_validation_blocks_approval_and_execution(api):
    client, _, runner = api
    migration = submit(client)
    assert (
        approve(
            client,
            migration,
            {"artifact_hash": migration["analysis"]["artifact_hash"], "id": "missing"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/migrations/{migration['id']}/execute", json={"approval_id": "missing"}
        ).status_code
        == 409
    )
    assert runner.calls == []


def test_approved_artifact_executes_once_and_audits(api):
    client, _, runner = api
    migration = submit(client)
    evidence = validate(client, migration)
    response = approve(client, migration, evidence)
    assert response.status_code == 201
    payload = {"approval_id": response.json()["id"]}
    endpoint = f"/api/migrations/{migration['id']}/execute"
    execution = client.post(endpoint, json=payload)
    assert execution.status_code == 202
    assert client.get(f"/api/jobs/{execution.json()['id']}").json()["status"] == "passed"
    assert client.post(endpoint, json=payload).status_code == 409
    assert len(runner.calls) == 2
    events = {item["event"] for item in client.get("/api/audit").json()}
    assert {"artifact.approved", "job.queued", "job.started", "job.passed"} <= events


def test_cross_migration_reports_and_approvals_reject(api):
    client, _, _ = api
    first, second = submit(client), submit(client)
    evidence = validate(client, first)
    assert approve(client, second, evidence).status_code == 409
    approval = approve(client, first, evidence).json()
    assert (
        client.post(
            f"/api/migrations/{second['id']}/execute", json={"approval_id": approval["id"]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("tamper", ["age", "digest", "failed_check", "empty_checks", "failed_job"])
def test_invalid_evidence_cannot_approve(api, tamper):
    client, application, _ = api
    migration = submit(client)
    evidence = validate(client, migration)
    with application.state.sessions.begin() as session:
        job = session.get(Job, evidence["id"])
        if tamper == "age":
            job.created_at = datetime.now(UTC) - timedelta(hours=2)
        elif tamper == "digest":
            job.artifact_hash = "0" * 64
        elif tamper == "failed_job":
            job.status = "failed"
        elif tamper == "failed_check":
            job.report = {**job.report, "checks": [{"name": "integrity", "passed": False}]}
        else:
            job.report = {**job.report, "checks": []}
    assert approve(client, migration, evidence).status_code == 409


def test_wrong_submitted_hash_rejects(api):
    client, _, _ = api
    migration = submit(client)
    evidence = validate(client, migration)
    assert approve(client, migration, evidence, artifact_hash="a" * 64).status_code == 409


def test_plan_changes_invalidate_evidence(api):
    client, application, _ = api
    migration = submit(client, "CREATE INDEX accounts_email_idx ON accounts(email)")
    client.post(f"/api/migrations/{migration['id']}/plan")
    evidence = validate(client, migration, "plan")
    with application.state.sessions.begin() as session:
        record = session.get(Migration, migration["id"])
        record.plan = {**record.plan, "warnings": ["changed review artifact"]}
    assert approve(client, migration, evidence).status_code == 409


def test_sql_changes_cannot_reuse_old_digest(api):
    client, application, _ = api
    migration = submit(client)
    evidence = validate(client, migration)
    with application.state.sessions.begin() as session:
        record = session.get(Migration, migration["id"])
        record.sql = "ALTER TABLE accounts ADD COLUMN substituted text"
    assert approve(client, migration, evidence).status_code == 409


def test_checks_are_revalidated_at_execution(api):
    client, application, runner = api
    migration = submit(client)
    evidence = validate(client, migration)
    approval = approve(client, migration, evidence).json()
    with application.state.sessions.begin() as session:
        job = session.get(Job, evidence["id"])
        job.report = {**job.report, "checks": []}
    assert (
        client.post(
            f"/api/migrations/{migration['id']}/execute", json={"approval_id": approval["id"]}
        ).status_code
        == 409
    )
    assert len(runner.calls) == 1


def test_cancel_running_job_and_conflicting_request(api):
    client, _, runner = api
    migration = submit(client)
    runner.block = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f"/api/migrations/{migration['id']}/validate")
        try:
            assert runner.started.wait(3)
            job = client.get("/api/jobs").json()[0]
            assert job["status"] == "running"
            assert client.post(f"/api/migrations/{migration['id']}/validate").status_code == 409
            assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        finally:
            runner.release.set()
        assert pending.result(timeout=5).status_code == 202
    finished = client.get(f"/api/jobs/{job['id']}").json()
    assert finished["status"] == "cancelled"
    assert approve(client, migration, finished).status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409


def test_restart_marks_interrupted_job_failed(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'restart.db'}", allow_sqlite=True, api_token=TOKEN
    )
    first = create_app(settings, MockUnitRunner())
    with TestClient(first) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        migration = submit(client)
        with first.state.sessions.begin() as session:
            interrupted = Job(
                id=new_id(),
                migration_id=migration["id"],
                kind="validation",
                artifact_hash=migration["analysis"]["artifact_hash"],
                status="running",
            )
            session.add(interrupted)
            interrupted_id = interrupted.id
    second = create_app(settings, MockUnitRunner())
    with TestClient(second) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        job = client.get(f"/api/jobs/{interrupted_id}").json()
        assert job["status"] == "failed"
        assert "no automatic replay" in job["report"]["error"]
        assert job["report"]["checks"] == []


def test_external_execution_arguments_reject(api):
    client, _, runner = api
    migration = submit(client)
    assert (
        client.post(
            f"/api/migrations/{migration['id']}/validate",
            json={"target": "migration", "database_url": "postgres://production"},
        ).status_code
        == 422
    )
    assert runner.calls == []
