"""Run reproducible local quality gates; --full never treats absent Docker as success."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require_test_evidence(result: dict, path: Path) -> dict:
    try:
        xml = ET.parse(path).getroot()
        cases = xml.findall(".//testcase")
        skipped = xml.findall(".//testcase/skipped")
        failures = xml.findall(".//testcase/failure") + xml.findall(".//testcase/error")
        result.update(test_count=len(cases), skipped_count=len(skipped), failed_count=len(failures))
        if not cases or skipped or failures:
            result["passed"] = False
            result["error"] = "Acceptance requires executed tests with zero failures and zero skips"
    except (OSError, ET.ParseError):
        result["passed"] = False
        result["error"] = "Missing or invalid machine-readable test evidence"
    return result


def docker_gate() -> dict:
    return run_gate(
        "Docker infrastructure",
        [
            sys.executable,
            "-c",
            "from schemashield.config import Settings; from schemashield.sandbox import SandboxRunner; "
            "r=SandboxRunner(Settings(sandbox_enabled=True)); c=r.capability(); "
            "print(c['reason']); raise SystemExit(0 if c['sandbox_available'] else 1)",
        ],
        timeout=30,
    )


def run_gate(name: str, command: list[str], *, timeout: int = 600) -> dict:
    started = datetime.now(UTC)
    print(f"Running {name}: {' '.join(command)}", flush=True)
    try:
        process = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if process.stdout:
            print(process.stdout, end="" if process.stdout.endswith("\n") else "\n")
        if process.stderr:
            print(process.stderr, file=sys.stderr)
        return {
            "name": name,
            "command": command,
            "passed": process.returncode == 0,
            "exit_code": process.returncode,
            "started_at": started.isoformat(),
            "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"FAILED {name}: {error}", file=sys.stderr)
        return {"name": name, "command": command, "passed": False, "error": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true", help="Require live Docker, integration and browser tests"
    )
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "verification.json")
    args = parser.parse_args()
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    unit_xml = ROOT / "reports" / "unit-junit.xml"
    unit_xml.parent.mkdir(parents=True, exist_ok=True)
    unit_xml.unlink(missing_ok=True)
    gates = [
        ("Python lint", [sys.executable, "-m", "ruff", "check", "."]),
        ("Python format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        (
            "Python unit tests",
            [
                sys.executable,
                "scripts/run_tests.py",
                "-m",
                "not integration",
                f"--junitxml={unit_xml}",
            ],
        ),
        ("Frontend lint", [npm, "run", "lint", "--prefix", "frontend"]),
        ("Frontend format", [npm, "run", "format:check", "--prefix", "frontend"]),
        ("Frontend build", [npm, "run", "build", "--prefix", "frontend"]),
        ("Static evaluation", [sys.executable, "scripts/evaluate.py"]),
        ("Frontend dependency audit", [npm, "audit", "--prefix", "frontend"]),
    ]
    results = [run_gate(name, command) for name, command in gates]
    require_test_evidence(next(r for r in results if r["name"] == "Python unit tests"), unit_xml)
    if args.full:
        docker_result = docker_gate()
        results.append(docker_result)
        if docker_result["passed"]:
            junit = ROOT / "reports" / "integration-junit.xml"
            junit.unlink(missing_ok=True)
            integration = run_gate(
                "PostgreSQL integration tests",
                [sys.executable, "scripts/container_verify.py", "--rebuild"],
                timeout=900,
            )
            results.append(require_test_evidence(integration, junit))
            results.append(
                run_gate(
                    "Deployed Python dependency audit",
                    [sys.executable, "scripts/container_verify.py", "--audit"],
                )
            )
            runtime_path = ROOT / "reports" / "runtime-evaluation.json"
            runtime_path.unlink(missing_ok=True)
            runtime = run_gate(
                "Runtime evaluation",
                [sys.executable, "scripts/container_verify.py", "--evaluation"],
                timeout=900,
            )
            try:
                evidence = json.loads(runtime_path.read_text(encoding="utf-8"))
                if (
                    evidence["status"] != "passed"
                    or not evidence["cases"]
                    or evidence.get("skipped_samples") != 0
                    or evidence.get("completed_samples") != evidence.get("planned_samples")
                ):
                    runtime["passed"] = False
                    runtime["error"] = "Complete empirical runtime evidence required"
            except (OSError, ValueError, KeyError):
                runtime["passed"] = False
                runtime["error"] = "Runtime evidence unavailable"
            results.append(runtime)
            browser_xml = ROOT / "reports" / "browser-junit.xml"
            browser_xml.unlink(missing_ok=True)
            browser = run_gate(
                "Browser workflows",
                [
                    sys.executable,
                    "scripts/container_demo.py",
                    "--test-browser",
                    "--rebuild-api",
                    "--rebuild-frontend",
                ],
                timeout=1200,
            )
            results.append(require_test_evidence(browser, browser_xml))
            compose_path = ROOT / "reports" / "compose-verification.json"
            compose_path.unlink(missing_ok=True)
            compose = run_gate(
                "Compose deployment", [sys.executable, "scripts/compose_verify.py"], timeout=1200
            )
            try:
                evidence = json.loads(compose_path.read_text(encoding="utf-8"))
                if (
                    evidence.get("status") != "passed"
                    or not evidence.get("checks")
                    or not all(c.get("passed") is True for c in evidence["checks"])
                ):
                    compose["passed"] = False
            except (OSError, ValueError):
                compose["passed"] = False
                compose["error"] = "Compose deployment evidence unavailable"
            results.append(compose)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "full" if args.full else "local",
        "passed": all(result["passed"] for result in results),
        "gates": results,
        "limitations": []
        if args.full
        else ["Local gates do not establish real PostgreSQL integration or browser acceptance"],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Verification {'passed' if report['passed'] else 'failed'}; report: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
