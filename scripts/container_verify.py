"""Build and run trusted verification code beside isolated PostgreSQL networks.

Uses the local Docker SDK, so a CLI is not required. The trusted verifier receives
the fixed Docker socket; migration containers never receive it. Source is uploaded
in a tar context containing only reviewed project directories and lock files.
"""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import time
import uuid
from pathlib import Path

import docker
from schemashield.config import Settings
from schemashield.sandbox import SandboxRunner

if __package__:
    from .build_context import source_files
else:
    from build_context import source_files

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "schemashield-verifier:local"
TRUSTED_OWNER = "io.schemashield.verifier"


def build(client, *, tag=IMAGE, dockerfile="backend/Dockerfile.verify"):
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w") as archive:
        for name in ("backend", "alembic", "scripts", "tests", "fixtures"):
            for path in source_files(ROOT, ROOT / name):
                archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        for name in ("pyproject.toml", "uv.lock", "alembic.ini"):
            archive.add(ROOT / name, arcname=name)
    context.seek(0)
    image, logs = client.images.build(
        fileobj=context,
        custom_context=True,
        dockerfile=dockerfile,
        tag=tag,
        rm=True,
    )
    for line in logs:
        if line.get("stream"):
            print(line["stream"], end="", flush=True)
    return image.id


def copy_reports(container, *, audit=False):
    try:
        chunks, _ = container.get_archive("/app/python-audit.json" if audit else "/app/reports")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
            for entry in archive:
                # Only flat known report files; never extract paths from Docker.
                basename = Path(entry.name).name
                if entry.isfile() and basename in {
                    "integration-junit.xml",
                    "runtime-evaluation.json",
                    "static-evaluation.json",
                    "python-audit.json",
                }:
                    source = archive.extractfile(entry)
                    if source is not None:
                        (ROOT / "reports" / basename).write_bytes(source.read())
    except docker.errors.NotFound:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--evaluation", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    runner = SandboxRunner(Settings(sandbox_enabled=True))
    client = runner.client(timeout=600)
    container = None
    try:
        if args.rebuild:
            build(client)
        else:
            try:
                client.images.get(IMAGE)
            except docker.errors.ImageNotFound:
                build(client)
        command = (
            [
                "python",
                "-m",
                "pip_audit",
                "--local",
                "--skip-editable",
                "--format",
                "json",
                "--output",
                "python-audit.json",
            ]
            if args.audit
            else ["python", "scripts/evaluate_runtime.py"]
            if args.evaluation
            else [
                "python",
                "scripts/run_tests.py",
                "tests/integration",
                "-m",
                "integration",
                "--junitxml=reports/integration-junit.xml",
            ]
        )
        container = client.containers.run(
            IMAGE,
            command=command,
            detach=True,
            labels={TRUSTED_OWNER: uuid.uuid4().hex},
            volumes={}
            if args.audit
            else {
                "/var/run/schemashield-docker.sock"
                if client.api.base_url.startswith("https://")
                else "/var/run/docker.sock": {"bind": "/run/docker.sock", "mode": "rw"}
            },
            environment={
                "SCHEMASHIELD_SANDBOX_TRANSPORT": "container-network",
                "SCHEMASHIELD_SANDBOX_ENABLED": "true",
                "DOCKER_HOST": "unix:///run/docker.sock",
            },
            mem_limit="768m",
            nano_cpus=2_000_000_000,
        )
        deadline = time.monotonic() + 900
        # Bound the trusted test process too, and preserve failed reports.
        while time.monotonic() < deadline:
            container.reload()
            if container.status in {"exited", "dead"}:
                break
            time.sleep(1)
        else:
            container.kill()
            raise RuntimeError("Verifier exceeded its 900-second budget")
        print(container.logs().decode("utf-8", errors="replace"))
        copy_reports(container, audit=args.audit)
        code = container.attrs["State"]["ExitCode"]
        print(
            json.dumps(
                {
                    "exit_code": code,
                    "image": IMAGE,
                    "mode": "audit"
                    if args.audit
                    else "evaluation"
                    if args.evaluation
                    else "integration",
                }
            )
        )
        return code
    finally:
        if container:
            container.remove(force=True, v=True)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
