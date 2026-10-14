"""Owned disposable PostgreSQL instances; never accepts an external connection target."""

import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import docker
import psycopg
from docker.errors import DockerException, NotFound

from .config import Settings
from .execution_guard import guard_sql

OWNER_LABEL = "io.schemashield.owner"
FIXTURE = """
CREATE TABLE accounts (id bigint PRIMARY KEY, email text NOT NULL,
 balance numeric(12,2) NOT NULL DEFAULT 0, touched integer NOT NULL DEFAULT 0);
INSERT INTO accounts SELECT i, 'account-' || i || '@example.test', i / 10.0, 0
FROM generate_series(1, 2000) AS i;
CREATE TABLE orders (id bigint PRIMARY KEY, account_id bigint);
INSERT INTO orders SELECT i, i FROM generate_series(1, 2000) AS i;
"""
FIXTURE_HASH = hashlib.sha256(FIXTURE.encode()).hexdigest()


class SandboxUnavailable(RuntimeError):
    pass


class SandboxCancelled(RuntimeError):
    pass


class SandboxRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.instance_id = hashlib.sha256(
            ("schemashield-owned-v1:" + settings.database_url).encode()
        ).hexdigest()[:24]

    def client(self, timeout=5):
        try:
            environment = dict(os.environ)
            if self.settings.docker_host:
                environment["DOCKER_HOST"] = self.settings.docker_host
            client = docker.from_env(timeout=timeout, environment=environment)
        except DockerException as exc:
            raise SandboxUnavailable("Local Docker connection failed") from exc
        local_socket = client.api.base_url in {
            "http+docker://localhost",
            "http+docker://localnpipe",
        }
        local_tls = (
            client.api.base_url == "https://127.0.0.1:2376"
            and bool(client.api.verify)
            and isinstance(client.api.cert, tuple)
            and len(client.api.cert) == 2
            and all(client.api.cert)
        )
        if not (local_socket or local_tls):
            client.close()
            raise SandboxUnavailable("Only a local Docker socket is authorized")
        return client

    def cleanup_expired(self):
        """Reap only owned resources with expired explicit leases, including after restart."""
        if not self.settings.sandbox_enabled:
            return
        client = None

        def expired(labels):
            if labels.get(OWNER_LABEL) != self.instance_id:
                return False
            try:
                return 0 < float(labels.get("io.schemashield.expires", "inf")) < time.time()
            except (ValueError, TypeError):
                return False

        try:
            client = self.client()
            for container in client.containers.list(
                all=True, filters={"label": f"{OWNER_LABEL}={self.instance_id}"}
            ):
                if expired(container.labels):
                    try:
                        container.remove(force=True, v=True)
                    except NotFound:
                        pass
            for network in client.networks.list(
                filters={"label": f"{OWNER_LABEL}={self.instance_id}"}
            ):
                labels = network.attrs.get("Labels", {})
                if expired(labels):
                    try:
                        network.remove()
                    except NotFound:
                        pass
        except (DockerException, SandboxUnavailable, ValueError):
            # A Docker outage cannot imply successful cleanup; worker reports disclose it.
            pass
        finally:
            if client:
                client.close()

    def capability(self) -> dict:
        if not self.settings.sandbox_enabled:
            return {
                "sandbox_enabled": False,
                "sandbox_available": False,
                "reason": "Disposable testing is disabled; enable only on a trusted local Docker host",
            }
        try:
            client = self.client(timeout=3)
            try:
                client.ping()
            finally:
                client.close()
            return {
                "sandbox_enabled": True,
                "sandbox_available": True,
                "reason": "Local Docker reachable; fixture validation does not establish production safety",
            }
        except (DockerException, SandboxUnavailable):
            return {
                "sandbox_enabled": True,
                "sandbox_available": False,
                "reason": "Local Docker is unavailable; no PostgreSQL validation has been performed",
            }

    @contextmanager
    def environment(self, cancelled: Callable[[], bool] = lambda: False):
        if not self.settings.sandbox_enabled:
            raise SandboxUnavailable("Sandbox execution is disabled")
        # Remote Docker targets violate the local-only trust boundary.
        client = self.client(timeout=5)
        container = None
        network = None
        timer = None
        attached_api = None
        password = secrets.token_urlsafe(32)
        deadline = time.monotonic() + self.settings.sandbox_timeout_seconds
        try:
            client.images.get(self.settings.sandbox_image)
            labels = {
                OWNER_LABEL: self.instance_id,
                "io.schemashield.expires": str(
                    time.time() + self.settings.sandbox_timeout_seconds + 10
                ),
            }
            network = client.networks.create(
                f"schemashield-{secrets.token_hex(8)}",
                internal=True,
                labels=labels,
            )
            container = client.containers.run(
                self.settings.sandbox_image,
                detach=True,
                environment={"POSTGRES_PASSWORD": password, "POSTGRES_DB": "sandbox"},
                ports={"5432/tcp": ("127.0.0.1", None)}
                if self.settings.sandbox_transport == "loopback"
                else {},
                network=network.name,
                labels=labels,
                mem_limit="384m",
                memswap_limit="384m",
                nano_cpus=1_000_000_000,
                pids_limit=80,
                security_opt=["no-new-privileges:true"],
                cap_drop=["NET_RAW", "NET_ADMIN", "SYS_ADMIN"],
                tmpfs={"/var/lib/postgresql/data": "rw,size=192m", "/tmp": "rw,size=16m"},
                command=[
                    "postgres",
                    "-c",
                    "max_connections=12",
                    "-c",
                    "shared_buffers=32MB",
                    "-c",
                    "statement_timeout=5000",
                    "-c",
                    "lock_timeout=1000",
                ],
            )

            # Lifetime bound survives cancellation of the calling thread.
            def expire():
                try:
                    container.remove(force=True, v=True)
                except DockerException:
                    pass

            timer = threading.Timer(self.settings.sandbox_timeout_seconds, expire)
            timer.daemon = True
            timer.start()
            if self.settings.sandbox_transport == "container-network":
                attached_api = os.environ.get("HOSTNAME")
                if not attached_api:
                    raise SandboxUnavailable(
                        "Container network transport requires an API container identity"
                    )
                network.connect(attached_api)
            admin = None
            while time.monotonic() < deadline:
                if cancelled():
                    raise SandboxCancelled("Cancelled while starting PostgreSQL")
                container.reload()
                bindings = container.attrs["NetworkSettings"]["Ports"].get("5432/tcp")
                network_host = container.attrs["NetworkSettings"]["Networks"][network.name].get(
                    "IPAddress"
                )
                if bindings or (attached_api and network_host):
                    port = 5432 if attached_api else int(bindings[0]["HostPort"])
                    host = network_host if attached_api else "127.0.0.1"
                    try:
                        admin = psycopg.connect(
                            host=host,
                            port=port,
                            dbname="sandbox",
                            user="postgres",
                            password=password,
                            connect_timeout=2,
                            autocommit=True,
                        )
                        break
                    except psycopg.OperationalError:
                        pass
                time.sleep(0.2)
            if admin is None:
                raise SandboxUnavailable("Disposable PostgreSQL startup exceeded its deadline")
            with admin:
                version = admin.execute("SHOW server_version_num").fetchone()[0]
                if not version.startswith("17"):
                    raise SandboxUnavailable("Unreviewed PostgreSQL major version")
                role_password = secrets.token_urlsafe(32)
                from psycopg import sql as pgsql

                admin.execute(
                    pgsql.SQL(
                        "CREATE ROLE migration LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                    ).format(pgsql.Literal(role_password))
                )
                admin.execute("REVOKE ALL ON DATABASE sandbox FROM PUBLIC")
                admin.execute("GRANT CONNECT ON DATABASE sandbox TO migration")
                admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                admin.execute("GRANT USAGE, CREATE ON SCHEMA public TO migration")
                admin.execute("SET ROLE migration")
                admin.execute(FIXTURE)
                admin.execute("RESET ROLE")
                connection_args = {
                    "host": host,
                    "port": port,
                    "dbname": "sandbox",
                    "user": "migration",
                    "password": role_password,
                    "connect_timeout": 2,
                }
                yield (
                    connection_args,
                    admin,
                    {
                        "postgres_version": version,
                        "image": self.settings.sandbox_image,
                        "image_id": container.image.id,
                        "fixture_hash": FIXTURE_HASH,
                        "fixture_rows": 2000,
                        "container_id": container.id[:12],
                    },
                )
        except docker.errors.ImageNotFound as exc:
            raise SandboxUnavailable(
                "Pinned image is missing; run docker pull postgres:17.11-bookworm"
            ) from exc
        except DockerException as exc:
            raise SandboxUnavailable(
                "Local Docker sandbox failed; inspect Docker service locally"
            ) from exc
        finally:
            if timer:
                timer.cancel()
            cleanup_failed = False
            if container:
                try:
                    container.remove(force=True, v=True)
                except NotFound:
                    pass
                except DockerException:
                    cleanup_failed = True
            if network:
                try:
                    if attached_api:
                        network.disconnect(attached_api, force=True)
                    network.remove()
                except NotFound:
                    pass
                except DockerException:
                    cleanup_failed = True
            client.close()
            if cleanup_failed:
                raise SandboxUnavailable(
                    "Sandbox resource cleanup failed; expired owned resources will be retried by the reaper"
                )

    def run(self, statements: list[str], cancelled: Callable[[], bool] = lambda: False) -> dict:
        for statement in statements:
            guard_sql(statement)
        started = time.monotonic()
        metrics = {
            "migration_duration_ms": 0.0,
            "application_queries": 0,
            "application_errors": 0,
            "lock_samples": [],
            "sample_interval_ms": 25,
            "statement_timings_ms": [],
            "instrumentation_errors": [],
        }
        stop = threading.Event()
        threads = []
        try:
            with self.environment(cancelled) as (args, admin, metadata):
                baseline = admin.execute(
                    "SELECT count(*), md5(string_agg(id::text || ':' || email || ':' || balance::text, ',' ORDER BY id)) FROM accounts"
                ).fetchone()
                traffic_ready = threading.Event()
                observer_ready = threading.Event()

                def traffic():
                    with psycopg.connect(
                        **args, autocommit=True, application_name="schemashield-traffic"
                    ) as connection:
                        connection.execute("SET statement_timeout='500ms'")
                        while not stop.is_set():
                            metrics["application_queries"] += 1
                            try:
                                connection.execute(
                                    "SELECT email FROM accounts WHERE id=1"
                                ).fetchone()
                                connection.execute(
                                    "UPDATE accounts SET touched=touched+1 WHERE id=1"
                                )
                            except psycopg.Error:
                                metrics["application_errors"] += 1
                            traffic_ready.set()
                            stop.wait(0.025)

                def sample():
                    with psycopg.connect(
                        **args, autocommit=True, application_name="schemashield-observer"
                    ) as connection:
                        connection.execute("SELECT count(*) FROM pg_locks").fetchone()
                        observer_ready.set()
                        while not stop.is_set():
                            rows = connection.execute(
                                "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid), extract(epoch FROM clock_timestamp()-query_start)*1000 FROM pg_stat_activity WHERE datname=current_database() AND application_name='schemashield-migration'"
                            ).fetchall()
                            if rows and len(metrics["lock_samples"]) < 4000:
                                locks = connection.execute(
                                    "SELECT mode, granted FROM pg_locks WHERE pid=%s AND locktype='relation'",
                                    (rows[0][0],),
                                ).fetchall()
                                metrics["lock_samples"].append(
                                    {
                                        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                        "wait_event_type": rows[0][1],
                                        "wait_event": rows[0][2],
                                        "blocking_pids": rows[0][3],
                                        "query_elapsed_ms": float(rows[0][4] or 0),
                                        "relation_locks": [
                                            {"mode": mode, "granted": granted}
                                            for mode, granted in locks
                                        ],
                                    }
                                )
                            stop.wait(0.025)

                def instrument(target):
                    try:
                        target()
                    except Exception as exc:
                        metrics["instrumentation_errors"].append(type(exc).__name__)

                for target in (traffic, sample):
                    thread = threading.Thread(target=instrument, args=(target,), daemon=True)
                    threads.append(thread)
                    thread.start()
                if not traffic_ready.wait(3) or not observer_ready.wait(3):
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=6)
                    raise SandboxUnavailable(
                        "Controlled workload instrumentation failed to initialize"
                    )
                migration_started = time.monotonic()
                try:
                    with psycopg.connect(
                        **args, autocommit=True, application_name="schemashield-migration"
                    ) as connection:
                        connection.execute("SET lock_timeout='1000ms'")
                        connection.execute("SET statement_timeout='5000ms'")
                        for statement in statements:
                            if cancelled():
                                raise SandboxCancelled(
                                    "Cancelled between bounded migration statements"
                                )
                            tick = time.monotonic()
                            connection.execute(statement)
                            metrics["statement_timings_ms"].append(
                                round((time.monotonic() - tick) * 1000, 3)
                            )
                finally:
                    metrics["migration_duration_ms"] = round(
                        (time.monotonic() - migration_started) * 1000, 3
                    )
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=6)
                after = admin.execute(
                    "SELECT count(*), md5(string_agg(id::text || ':' || email || ':' || balance::text, ',' ORDER BY id)) FROM accounts"
                ).fetchone()
                checks = [
                    {
                        "name": "fixture_row_count_and_original_values",
                        "passed": baseline == after,
                        "before_count": baseline[0],
                        "after_count": after[0],
                    }
                ]
                schema = admin.execute(
                    "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"
                ).fetchall()
                invalid_indexes = admin.execute(
                    "SELECT count(*) FROM pg_index WHERE NOT indisvalid"
                ).fetchone()[0]
                checks.append({"name": "all_indexes_valid", "passed": invalid_indexes == 0})
                unvalidated = admin.execute(
                    "SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated"
                ).fetchone()[0]
                checks.append(
                    {"name": "all_installed_constraints_validated", "passed": unvalidated == 0}
                )
                checks.append(
                    {
                        "name": "controlled_application_queries_succeeded",
                        "passed": metrics["application_errors"] == 0
                        and metrics["application_queries"] > 0
                        and not metrics["instrumentation_errors"],
                    }
                )
                metrics["application_error_rate"] = metrics["application_errors"] / max(
                    1, metrics["application_queries"]
                )
                metrics["observed_lock_wait_samples"] = sum(
                    s["wait_event_type"] == "Lock" for s in metrics["lock_samples"]
                )
                metrics["observed_lock_wait_duration_ms"] = (
                    metrics["observed_lock_wait_samples"] * metrics["sample_interval_ms"]
                )
                metrics["total_duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                return {
                    "status": "passed" if all(c["passed"] for c in checks) else "failed",
                    "checks": checks,
                    "metrics": metrics,
                    "environment": metadata,
                    "schema": [{"table": t, "column": c, "type": d} for t, c, d in schema],
                    "limitations": [
                        "Fixed 2000-row fixture; application cutover not tested",
                        "Sampled lock waits have 25ms resolution; short locks may be missed",
                        "Each statement autocommits; failed migrations may be partially applied",
                        "Disposable database is removed after each report",
                    ],
                }
        except SandboxCancelled:
            return {
                "status": "cancelled",
                "checks": [],
                "metrics": metrics,
                "error": "Cancelled; sandbox removed; partially committed steps are not rolled back",
            }
        except (psycopg.Error, SandboxUnavailable) as exc:
            # Never return DB connection strings, credentials, or arbitrary SQL error text.
            return {
                "status": "failed",
                "checks": [],
                "metrics": metrics,
                "error": f"{type(exc).__name__}: disposable validation failed; sandbox cleanup attempted",
                "sqlstate": getattr(exc, "sqlstate", None),
            }
        finally:
            stop.set()
