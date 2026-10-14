"""Verify the actual Compose deployment using a pinned registry CLI container.

The local host does not need a Docker CLI. Build source is uploaded without
secrets/dependencies. The unique Compose project and its own demo volume are
removed after checks; no existing user's project or volumes are adopted.
"""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import docker
import httpx
from schemashield.config import Settings
from schemashield.sandbox import SandboxRunner

if __package__:
    from .build_context import source_files
else:
    from build_context import source_files

ROOT = Path(__file__).resolve().parents[1]
CLI_IMAGE = "docker:28.5.2-cli"


def source_archive():
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w") as archive:
        for name in ("backend", "alembic", "frontend"):
            for path in source_files(ROOT, ROOT / name):
                if "evidence" not in path.relative_to(ROOT).parts:
                    archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        for name in (
            "pyproject.toml",
            "uv.lock",
            "alembic.ini",
            "compose.yaml",
            "compose.sandbox.yaml",
        ):
            archive.add(ROOT / name, arcname=name)
    return context.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    client = SandboxRunner(Settings(sandbox_enabled=True)).client(timeout=600)
    driver = None
    project = "schemashieldverify" + uuid.uuid4().hex[:12]
    prefix = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.sandbox.yaml",
    ]
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "cli_image": CLI_IMAGE,
        "project": project,
        "status": "failed",
        "checks": [],
    }
    try:
        try:
            client.images.get(CLI_IMAGE)
        except docker.errors.ImageNotFound:
            print("Pulling pinned Compose CLI image", flush=True)
            client.images.pull(CLI_IMAGE)
        driver = client.containers.run(
            CLI_IMAGE,
            entrypoint=["sh", "-c"],
            command=["mkdir -p /workspace && sleep 900"],
            detach=True,
            working_dir="/workspace",
            labels={"io.schemashield.compose-driver": project},
            environment={"DOCKER_HOST": "unix:///run/docker.sock", "DOCKER_BUILDKIT": "0"},
            volumes={
                "/var/run/schemashield-docker.sock"
                if client.api.base_url.startswith("https://")
                else "/var/run/docker.sock": {"bind": "/run/docker.sock", "mode": "rw"}
            },
        )
        driver.put_archive("/workspace", source_archive())
        config = driver.exec_run([*prefix, "config", "--quiet"])
        report["checks"].append({"name": "compose_config", "passed": config.exit_code == 0})
        if config.exit_code:
            raise RuntimeError("Compose configuration rejected")
        print("Starting unique Compose project with production images", flush=True)
        startup = driver.exec_run([*prefix, "up", "--build", "--wait", "--wait-timeout", "120"])
        # Docker build output contains no configured secrets, but startup output may.
        report["checks"].append({"name": "compose_up_build_wait", "passed": startup.exit_code == 0})
        if startup.exit_code:
            raise RuntimeError("Compose startup failed; inspect owned project locally")
        with httpx.Client(
            timeout=3, headers={"Authorization": "Bearer local-development-token-change-me"}
        ) as api:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if api.get("http://localhost:3000").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            ready = api.get("http://localhost:8000/ready").json()
            capability = api.get("http://localhost:8000/api/capabilities").json()
            report["checks"].extend(
                [
                    {
                        "name": "postgresql_persistence",
                        "passed": ready.get("persistence") == "postgresql",
                    },
                    {
                        "name": "isolated_runtime_available",
                        "passed": capability.get("sandbox_available") is True,
                    },
                    {
                        "name": "dashboard_http",
                        "passed": api.get("http://localhost:3000").status_code == 200,
                    },
                ]
            )
            unsafe = api.post(
                "http://localhost:8000/api/migrations",
                json={"title": "Compose destructive case", "sql": "DROP TABLE accounts"},
            )
            record = unsafe.json()
            rejected = api.post(f"http://localhost:8000/api/migrations/{record['id']}/validate")
            report["checks"].append(
                {
                    "name": "unsafe_execution_blocked",
                    "passed": unsafe.status_code == 201
                    and record["analysis"]["decision"] == "blocked"
                    and rejected.status_code == 409,
                }
            )
        report["status"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
        return 0 if report["status"] == "passed" else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: Compose verification failed"
        raise
    finally:
        if driver:
            result = driver.exec_run([*prefix, "down", "--volumes", "--remove-orphans"])
            report["checks"].append(
                {"name": "unique_project_cleanup", "passed": result.exit_code == 0}
            )
            if result.exit_code:
                report["status"] = "failed"
            driver.remove(force=True, v=True)
        client.close()
        (ROOT / "reports" / "compose-verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report))
        if driver and report["checks"][-1]["passed"] is not True:
            raise RuntimeError("Compose cleanup failed; verification cannot succeed")


if __name__ == "__main__":
    raise SystemExit(main())
