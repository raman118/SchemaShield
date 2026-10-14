import pytest
from pydantic import ValidationError
from schemashield.contracts import ApprovalRequest, SubmitMigration


def test_api_rejects_execution_connection_input():
    with pytest.raises(ValidationError):
        SubmitMigration(title="unsafe", sql="SELECT 1", database_url="postgres://production")


def test_approval_requires_digest_and_report():
    with pytest.raises(ValidationError):
        ApprovalRequest(artifact_hash="old", report_id="x", reviewer="operator")
