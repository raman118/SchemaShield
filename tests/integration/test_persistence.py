import pytest
from fastapi.testclient import TestClient
from schemashield.main import WorkerConflict, create_app
from schemashield.store import Job, new_id
from sqlalchemy import text

pytestmark = pytest.mark.integration


def test_alembic_postgresql_persistence_survives_app_restart(real_control):
    settings, metadata = real_control
    assert metadata["postgres_version"].startswith("17")
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/migrations",
            headers=headers,
            json={
                "title": "Persist PostgreSQL",
                "sql": "ALTER TABLE accounts ADD COLUMN nickname text",
            },
        )
        assert response.status_code == 201
        record = response.json()
        assert client.get("/ready").json()["persistence"] == "postgresql"
    with TestClient(create_app(settings)) as restarted:
        fetched = restarted.get(f"/api/migrations/{record['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["analysis"] == record["analysis"]
        assert any(
            event["event"] == "migration.analyzed"
            for event in restarted.get("/api/audit", headers=headers).json()
        )


def test_second_worker_cannot_recover_live_jobs(real_control):
    settings, _ = real_control
    app = create_app(settings)
    with TestClient(app):
        job_id = new_id()
        with app.state.sessions.begin() as session:
            session.add(
                Job(
                    id=job_id,
                    migration_id=new_id(),
                    kind="validation",
                    artifact_hash="a" * 64,
                    status="running",
                )
            )
        with pytest.raises(WorkerConflict, match="one API worker"):
            with TestClient(create_app(settings)):
                pytest.fail("A second worker must not start")
        with app.state.sessions() as session:
            assert session.get(Job, job_id).status == "running"
    restarted = create_app(settings)
    with TestClient(restarted):
        with restarted.state.sessions() as session:
            assert session.get(Job, job_id).status == "failed"


def test_lost_worker_lease_fails_closed_until_restart(real_control):
    settings, _ = real_control
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(app) as client:
        with app.state.sessions.begin() as session:
            assert session.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": app.state.worker_lease_pid}
            ).scalar_one()
        assert client.get("/ready").status_code == 503
        assert (
            client.post(
                "/api/migrations",
                headers=headers,
                json={"title": "Stopped", "sql": "CREATE TABLE x(id bigint)"},
            ).status_code
            == 503
        )
        assert client.get("/health").status_code == 200
        with TestClient(create_app(settings)) as replacement:
            assert replacement.get("/ready").status_code == 200
        assert client.get("/ready").status_code == 503
