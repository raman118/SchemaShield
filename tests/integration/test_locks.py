import json
import threading
import time

import psycopg
import pytest
from schemashield.execution_guard import guard_sql

pytestmark = pytest.mark.integration


def test_real_access_exclusive_wait_has_blocking_session(real_runner, tmp_path):
    statement = guard_sql("ALTER TABLE accounts ADD COLUMN contention_probe text")[0]
    with real_runner.environment() as (arguments, admin, metadata):
        holder = psycopg.connect(**arguments, application_name="controlled-lock-holder")
        holder.execute("SELECT id FROM accounts WHERE id=1").fetchone()
        holder_pid = holder.info.backend_pid
        started = threading.Event()
        result = {}

        def migrate():
            try:
                with psycopg.connect(
                    **arguments, autocommit=True, application_name="controlled-lock-migration"
                ) as connection:
                    connection.execute("SET lock_timeout='1000ms'")
                    result["migration_pid"] = connection.info.backend_pid
                    tick = time.monotonic()
                    started.set()
                    connection.execute(statement)
                    result["elapsed_ms"] = (time.monotonic() - tick) * 1000
            except psycopg.Error as error:
                result["sqlstate"] = error.sqlstate

        worker = threading.Thread(target=migrate, daemon=True)
        worker.start()
        observed = None
        try:
            assert started.wait(3)
            deadline = time.monotonic() + 0.7
            while time.monotonic() < deadline:
                row = admin.execute(
                    "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid=%s",
                    (result["migration_pid"],),
                ).fetchone()
                if row and row[0] == "Lock" and holder_pid in row[1]:
                    locks = admin.execute(
                        "SELECT mode, granted FROM pg_locks WHERE pid=%s AND locktype='relation'",
                        (result["migration_pid"],),
                    ).fetchall()
                    if ("AccessExclusiveLock", False) in locks:
                        observed = {
                            "wait_event_type": row[0],
                            "blocking_pids": row[1],
                            "locks": locks,
                        }
                        break
                time.sleep(0.01)
        finally:
            holder.rollback()
            holder.close()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert observed is not None, result
        assert "sqlstate" not in result, result
        assert result["elapsed_ms"] > 0
        assert admin.execute("SELECT count(*) FROM accounts").fetchone()[0] == 2000
        (tmp_path / "real-lock-measurement.json").write_text(
            json.dumps(
                {
                    "environment": metadata,
                    "observed": observed,
                    "measurement": result,
                    "limitations": "One controlled ACCESS SHARE blocker, 10ms polling; elapsed time includes lock wait and DDL.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
