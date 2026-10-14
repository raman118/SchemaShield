import json
import sys

from scripts import verify


def test_run_gate_records_real_success_and_failure():
    passed = verify.run_gate("success", [sys.executable, "-c", "print('verified')"])
    failed = verify.run_gate("failure", [sys.executable, "-c", "raise SystemExit(7)"])
    assert passed["passed"] is True
    assert failed["passed"] is False
    assert failed["exit_code"] == 7


def test_full_verification_requires_docker(tmp_path, monkeypatch):
    report = tmp_path / "verification.json"
    monkeypatch.setattr(sys, "argv", ["verify", "--full", "--report", str(report)])
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(
        verify,
        "docker_gate",
        lambda: {
            "name": "Docker infrastructure",
            "passed": False,
            "error": "Local Docker unavailable",
        },
    )
    monkeypatch.setattr(
        verify,
        "run_gate",
        lambda name, command, **kwargs: {
            "name": name,
            "command": command,
            "passed": True,
        },
    )
    assert verify.main() == 1
    evidence = json.loads(report.read_text())
    assert evidence["passed"] is False
    assert evidence["gates"][-1]["name"] == "Docker infrastructure"
    assert "unavailable" in evidence["gates"][-1]["error"]


def test_full_verification_rejects_skipped_integration(tmp_path, monkeypatch):
    report = tmp_path / "verification.json"
    monkeypatch.setattr(sys, "argv", ["verify", "--full", "--report", str(report)])
    monkeypatch.setattr(verify, "ROOT", tmp_path)
    monkeypatch.setattr(verify.shutil, "which", lambda name: name)

    def gate(name, command, **kwargs):
        if name == "PostgreSQL integration tests":
            (tmp_path / "reports" / "integration-junit.xml").write_text(
                '<testsuites><testsuite><testcase name="unavailable"><skipped/></testcase>'
                "</testsuite></testsuites>",
                encoding="utf-8",
            )
        return {"name": name, "command": command, "passed": True}

    monkeypatch.setattr(verify, "run_gate", gate)
    assert verify.main() == 1
    evidence = json.loads(report.read_text())
    integration = next(
        gate for gate in evidence["gates"] if gate["name"] == "PostgreSQL integration tests"
    )
    assert integration["passed"] is False
    assert integration["skipped_count"] == 1


def test_machine_reports_require_executed_passing_tests(tmp_path):
    report = tmp_path / "browser.xml"
    for xml in [
        "<testsuites/>",
        "<testsuites><testcase><skipped/></testcase></testsuites>",
        "<testsuites><testcase><failure/></testcase></testsuites>",
        "<testsuites><testcase><error/></testcase></testsuites>",
    ]:
        report.write_text(xml, encoding="utf-8")
        assert verify.require_test_evidence({"passed": True}, report)["passed"] is False
    report.write_text(
        '<testsuites><testsuite><testcase name="actual"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert verify.require_test_evidence({"passed": True}, report)["passed"] is True
