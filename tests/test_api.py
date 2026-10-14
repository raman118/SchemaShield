import pytest
from fastapi.testclient import TestClient
from schemashield.config import Settings
from schemashield.main import create_app

TOKEN = "Bearer test-token-with-at-least-24-chars"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        allow_sqlite=True,
        api_token=TOKEN.removeprefix("Bearer "),
        sandbox_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = TOKEN
        yield client


def test_health_and_capabilities(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 200
    capability = client.get("/api/capabilities").json()
    assert capability["sandbox_available"] is False
    assert capability["production_execution"] is False


def test_analysis_persistence_plan_and_audit(client):
    result = client.post(
        "/api/migrations", json={"title": "Unsafe", "sql": "ALTER TABLE accounts DROP COLUMN email"}
    )
    assert result.status_code == 201
    record = result.json()
    assert record["analysis"]["decision"] == "blocked"
    assert record["analysis"]["tested"] is False
    assert client.get(f"/api/migrations/{record['id']}").json()["analysis"] == record["analysis"]
    assert len(client.get("/api/migrations").json()) == 1
    assert client.post(f"/api/migrations/{record['id']}/validate").status_code == 409
    assert client.post(f"/api/migrations/{record['id']}/plan").status_code == 200
    assert client.get("/api/audit").json()[0]["event"] == "plan.generated"


def test_missing_sandbox_never_creates_pass_report(client):
    record = client.post(
        "/api/migrations",
        json={"title": "Add", "sql": "ALTER TABLE accounts ADD COLUMN nickname text"},
    ).json()
    response = client.post(f"/api/migrations/{record['id']}/validate")
    assert response.status_code == 503
    assert client.get("/api/jobs").json() == []


def test_invalid_sql_is_persisted_as_blocked_diagnostics(client):
    record = client.post(
        "/api/migrations", json={"title": "Invalid", "sql": "ALTER NOT SQL"}
    ).json()
    assert record["analysis"]["decision"] == "blocked"
    assert record["analysis"]["parsed"]["valid"] is False


def test_unknown_and_oversized_requests(client):
    assert (
        client.post(
            "/api/migrations",
            json={"title": "X", "sql": "SELECT 1", "database_url": "postgres://production"},
        ).status_code
        == 422
    )
    assert client.post("/api/migrations", content=b"x" * 120001).status_code == 413
    assert client.get("/api/migrations/missing").status_code == 404


def test_authentication_applies_to_history(client):
    assert (
        client.get("/api/migrations", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    assert (
        client.post(
            "/api/migrations", headers={"Authorization": ""}, json={"title": "X", "sql": "SELECT 1"}
        ).status_code
        == 401
    )
