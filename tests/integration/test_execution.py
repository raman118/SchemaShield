import pytest
from fastapi.testclient import TestClient
from schemashield.main import create_app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "source,target",
    [
        ("ALTER TABLE accounts ADD COLUMN nickname text", "migration"),
        ("CREATE INDEX accounts_email_idx ON accounts(email)", "plan"),
        ("ALTER TABLE accounts ADD CONSTRAINT balance_nonnegative CHECK(balance>=0)", "plan"),
        (
            "ALTER TABLE orders ADD CONSTRAINT orders_account_fk FOREIGN KEY(account_id) REFERENCES accounts(id)",
            "plan",
        ),
    ],
)
def test_real_validation_approval_execution_data_integrity(real_control, source, target):
    settings, _ = real_control
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        migration = client.post(
            "/api/migrations", json={"title": "Real approved execution", "sql": source}
        ).json()
        endpoint = f"/api/migrations/{migration['id']}"
        if target == "plan":
            assert migration["analysis"]["decision"] == "blocked"
            assert client.post(endpoint + "/validate").status_code == 409
            plan = client.post(endpoint + "/plan").json()
            assert plan["eligible"]
            if "ADD CONSTRAINT" in source:
                assert "NOT VALID" in plan["steps"][0]["sql"]
                assert any(
                    step["phase"] == "VERIFY" and "VALIDATE CONSTRAINT" in (step["sql"] or "")
                    for step in plan["steps"]
                )
        validation = client.post(endpoint + "/validate", json={"target": target})
        assert validation.status_code == 202, validation.text
        report = client.get(f"/api/jobs/{validation.json()['id']}").json()
        assert report["status"] == "passed", report
        assert all(check["passed"] for check in report["report"]["checks"])
        assert report["report"]["metrics"]["application_queries"] > 0
        if "ADD CONSTRAINT" in source:
            assert len(report["report"]["metrics"]["statement_timings_ms"]) == 2
            assert any(
                check["name"] == "all_installed_constraints_validated" and check["passed"]
                for check in report["report"]["checks"]
            )
        approval = client.post(
            endpoint + "/approve",
            json={
                "artifact_hash": report["artifact_hash"],
                "report_id": report["id"],
                "reviewer": "integration-human-reviewer",
            },
        )
        assert approval.status_code == 201, approval.text
        approved = {"approval_id": approval.json()["id"]}
        execution = client.post(endpoint + "/execute", json=approved)
        assert execution.status_code == 202, execution.text
        executed = client.get(f"/api/jobs/{execution.json()['id']}").json()
        assert executed["status"] == "passed", executed
        if "ADD CONSTRAINT" in source:
            assert len(executed["report"]["metrics"]["statement_timings_ms"]) == 2
            assert any(
                check["name"] == "all_installed_constraints_validated" and check["passed"]
                for check in executed["report"]["checks"]
            )
        assert any(
            check["name"] == "fixture_row_count_and_original_values"
            and check["passed"]
            and check["before_count"] == check["after_count"] == 2000
            for check in executed["report"]["checks"]
        )
        assert client.post(endpoint + "/execute", json=approved).status_code == 409
