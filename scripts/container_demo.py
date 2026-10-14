"""Run the real local demo or browser gate using trusted, owned Docker services.

Control persistence and execution PostgreSQL are separate containers. Demo
control records are disposable and removed on exit; Compose provides persistent
control storage. No host source directory or credential files enter containers.
"""

from __future__ import annotations

import argparse
import io
import secrets
import tarfile
import time
import uuid
from pathlib import Path

import docker
import httpx
from schemashield.config import Settings
from schemashield.sandbox import SandboxRunner

if __package__:
    from .build_context import source_files
    from .container_verify import build
else:
    from build_context import source_files
    from container_verify import build

ROOT = Path(__file__).resolve().parents[1]
API_IMAGE = "schemashield-api:local"
WEB_IMAGE = "schemashield-dashboard:local-verified"
BROWSER_IMAGE = "schemashield-browser:local-verified"


def build_frontend(client, *, browser=False):
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w") as archive:
        for path in source_files(ROOT, ROOT / "frontend"):
            if "evidence" not in path.relative_to(ROOT).parts:
                archive.add(
                    path, arcname=path.relative_to(ROOT / "frontend").as_posix(), recursive=False
                )
    context.seek(0)
    _, logs = client.images.build(
        fileobj=context,
        custom_context=True,
        dockerfile="Dockerfile.e2e" if browser else "Dockerfile",
        tag=BROWSER_IMAGE if browser else WEB_IMAGE,
        rm=True,
    )
    for line in logs:
        if line.get("stream"):
            print(line["stream"], end="", flush=True)


def export_browser(container):
    chunks, _ = container.get_archive("/app/evidence")
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
        for entry in archive:
            name = Path(entry.name).name
            if entry.isfile() and name in {
                "browser-junit.xml",
                "dashboard.png",
                "runtime-backfill.png",
                "runtime-validation.png",
            }:
                stream = archive.extractfile(entry)
                if stream:
                    destination = (
                        ROOT / ("reports" if name.endswith(".xml") else "frontend/evidence") / name
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(stream.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-browser", action="store_true")
    parser.add_argument("--rebuild-api", action="store_true")
    parser.add_argument("--rebuild-frontend", action="store_true")
    parser.add_argument(
        "--duration", type=int, default=1800, help="Demo lifetime in seconds (60..3600)"
    )
    args = parser.parse_args()
    if not 60 <= args.duration <= 3600:
        parser.error("duration must be 60..3600")
    local_runner = SandboxRunner(Settings(sandbox_enabled=True))
    client = local_runner.client(timeout=600)
    owned = []
    network = None
    try:
        for image, browser in [
            (WEB_IMAGE, False),
            *(([(BROWSER_IMAGE, True)]) if args.test_browser else []),
        ]:
            if args.rebuild_frontend:
                build_frontend(client, browser=browser)
            else:
                try:
                    client.images.get(image)
                except docker.errors.ImageNotFound:
                    build_frontend(client, browser=browser)
        if args.rebuild_api:
            build(client, tag=API_IMAGE, dockerfile="backend/Dockerfile")
        else:
            try:
                client.images.get(API_IMAGE)
            except docker.errors.ImageNotFound:
                build(client, tag=API_IMAGE, dockerfile="backend/Dockerfile")
        identity = uuid.uuid4().hex
        labels = {"io.schemashield.demo": identity}
        network = client.networks.create("schemashield-demo-" + identity[:12], labels=labels)
        password = secrets.token_urlsafe(32)
        control = client.containers.run(
            "postgres:17.11-bookworm",
            detach=True,
            labels=labels,
            network=network.name,
            environment={
                "POSTGRES_USER": "schemashield",
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": "control",
            },
            mem_limit="384m",
            nano_cpus=1_000_000_000,
            tmpfs={"/var/lib/postgresql/data": "rw,size=192m"},
        )
        owned.append(control)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (
                control.exec_run(["pg_isready", "-U", "schemashield", "-d", "control"]).exit_code
                == 0
            ):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Control PostgreSQL failed to initialize")
        database_url = f"postgresql+psycopg://schemashield:{password}@{control.id[:12]}/control"
        # Fixed local development credential matches the documented browser demo.
        # This is never a production credential; Compose supports operator tokens.
        api = client.containers.run(
            API_IMAGE,
            detach=True,
            labels=labels,
            network=network.name,
            environment={
                "SCHEMASHIELD_DATABASE_URL": database_url,
                "SCHEMASHIELD_SANDBOX_ENABLED": "true",
                "SCHEMASHIELD_SANDBOX_TRANSPORT": "container-network",
                "DOCKER_HOST": "unix:///run/docker.sock",
            },
            ports={"8000/tcp": ("127.0.0.1", 8000), "3000/tcp": ("127.0.0.1", 3000)},
            volumes={
                "/var/run/schemashield-docker.sock"
                if client.api.base_url.startswith("https://")
                else "/var/run/docker.sock": {"bind": "/run/docker.sock", "mode": "rw"}
            },
            mem_limit="768m",
            nano_cpus=2_000_000_000,
        )
        owned.append(api)
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=2) as probe:
            while time.monotonic() < deadline:
                api.reload()
                if api.status == "exited":
                    # Service logs could contain administrator configuration; do not print them.
                    raise RuntimeError(
                        "API startup failed; inspect the owned API container locally"
                    )
                try:
                    if probe.get("http://127.0.0.1:8000/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("API readiness exceeded its deadline")
        web = client.containers.run(
            WEB_IMAGE,
            detach=True,
            labels=labels,
            network_mode=f"container:{api.id}",
            mem_limit="512m",
            nano_cpus=1_000_000_000,
        )
        owned.append(web)
        print(
            "Real PostgreSQL API: http://localhost:8000; dashboard: http://localhost:3000",
            flush=True,
        )
        if args.test_browser:
            browser = client.containers.run(
                BROWSER_IMAGE,
                detach=True,
                labels=labels,
                network_mode=f"container:{api.id}",
                command=["npm", "run", "test:e2e", "--", "--reporter=list,junit"],
                environment={
                    "SCHEMASHIELD_E2E_EXTERNAL": "1",
                    "SCHEMASHIELD_E2E_RUNTIME": "1",
                    "PLAYWRIGHT_JUNIT_OUTPUT_FILE": "/app/evidence/browser-junit.xml",
                },
                mem_limit="1536m",
                nano_cpus=2_000_000_000,
                shm_size="256m",
            )
            owned.append(browser)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                browser.reload()
                if browser.status in {"exited", "dead"}:
                    break
                time.sleep(1)
            else:
                browser.kill()
                raise RuntimeError("Browser gate exceeded 600 seconds")
            print(browser.logs().decode("utf-8", errors="replace"))
            export_browser(browser)
            return browser.attrs["State"]["ExitCode"]
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            time.sleep(1)
        return 0
    finally:
        for container in reversed(owned):
            container.remove(force=True, v=True)
        if network:
            network.remove()
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Owned demo services removed.")
