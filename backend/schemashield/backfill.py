"""Bounded, restartable fixture backfill on a caller-owned disposable connection.

This copies accounts.email to accounts.email_shadow only. General transformations
require reviewed SQL and application dual writes, which this engine does not install.
Progress lives inside the disposable database; it survives worker restarts, not
the intentional destruction of that database by SandboxRunner.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus

TRANSFORM = "accounts-email-shadow-v1"
ADVISORY_LOCK = 0x534348454D414246
RETRYABLE_STATES = frozenset({"40001", "40P01", "55P03", "57014"})
STATE_COLUMNS = "status,last_id,row_updates,batches,rescans,verified_rows,error_code"
STATE_DDL = """
CREATE TABLE IF NOT EXISTS public.schemashield_backfill (
 transform text PRIMARY KEY,
 status text NOT NULL CHECK (status IN ('running','paused','cancelled','failed','completed')),
 last_id bigint NOT NULL DEFAULT 0,
 row_updates bigint NOT NULL DEFAULT 0,
 batches integer NOT NULL DEFAULT 0,
 rescans integer NOT NULL DEFAULT 0,
 verified_rows bigint,
 error_code text,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
"""


class BackfillError(RuntimeError):
    pass


class BackfillBusy(BackfillError):
    pass


@dataclass(frozen=True)
class BackfillOptions:
    batch_size: int = 100
    max_retries: int = 3
    max_rescans: int = 3
    max_runtime_seconds: int = 30

    def __post_init__(self):
        limits = {
            "batch_size": (1, 1000),
            "max_retries": (0, 5),
            "max_rescans": (0, 10),
            "max_runtime_seconds": (1, 180),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def retryable(error: psycopg.Error) -> bool:
    return error.sqlstate in RETRYABLE_STATES


class BackfillEngine:
    def __init__(
        self,
        connection: psycopg.Connection,
        *,
        batch_size: int = 100,
        max_retries: int = 3,
        max_rescans: int = 3,
        max_runtime_seconds: int = 30,
    ):
        self.connection = connection
        self._rescan_base = 0
        self.options = BackfillOptions(batch_size, max_retries, max_rescans, max_runtime_seconds)
        if (
            not connection.autocommit
            or connection.info.transaction_status != TransactionStatus.IDLE
        ):
            raise BackfillError("Backfill requires an idle autocommit disposable connection")

    def _state(self) -> dict:
        row = self.connection.execute(
            f"SELECT {STATE_COLUMNS} FROM public.schemashield_backfill WHERE transform=%s",
            (TRANSFORM,),
        ).fetchone()
        if row is None:
            raise BackfillError("Backfill progress is missing")
        result = dict(zip(STATE_COLUMNS.split(","), row, strict=True))
        return {
            **result,
            "transform": TRANSFORM,
            "batch_size": self.options.batch_size,
            "verification": {
                "performed": result["status"] == "completed",
                "matching_rows": result["verified_rows"],
                "scope": "Point-in-time equality of every fixture source/target value",
            },
            "limitations": [
                "Only the fixed accounts.email to email_shadow text copy is supported",
                "Application dual writes are required after verification; later writes can invalidate equality",
                "Progress survives reconnects while this disposable database exists, not container destruction",
                "Rescans may update a row again; row_updates is an update count, not unique records",
            ],
        }

    def _status(self, status: str, error_code: str | None = None):
        self.connection.execute(
            "UPDATE public.schemashield_backfill SET status=%s,error_code=%s,"
            "verified_rows=NULL,"
            "updated_at=clock_timestamp() WHERE transform=%s",
            (status, error_code, TRANSFORM),
        )

    def _initialize(self):
        # No connection target is accepted here. The caller must obtain it from SandboxRunner.
        identity = self.connection.execute("SELECT current_database(),current_user").fetchone()
        if identity != ("sandbox", "migration"):
            raise BackfillError("Only the restricted disposable sandbox role is accepted")
        columns = self.connection.execute(
            "SELECT column_name,data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='accounts' "
            "AND column_name IN ('id','email','email_shadow')"
        ).fetchall()
        if dict(columns) != {"id": "bigint", "email": "text", "email_shadow": "text"}:
            raise BackfillError("Fixture needs bigint id and text email/email_shadow columns")
        primary = self.connection.execute(
            "SELECT array_agg(a.attname ORDER BY k.ordinality) FROM pg_index i "
            "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality) "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
            "WHERE i.indrelid='public.accounts'::regclass AND i.indisprimary"
        ).fetchone()[0]
        if primary != ["id"]:
            raise BackfillError("Fixture requires the id primary key")
        bad_ids, rows = self.connection.execute(
            "SELECT count(*) FILTER (WHERE id<=0),count(*) FROM public.accounts"
        ).fetchone()
        if bad_ids or rows > 10000:
            raise BackfillError("Fixture requires positive ids and at most 10000 records")
        with self.connection.transaction():
            self.connection.execute("SET LOCAL lock_timeout='1000ms'")
            self.connection.execute("SET LOCAL statement_timeout='5000ms'")
            self.connection.execute(STATE_DDL)
            self.connection.execute(
                "INSERT INTO public.schemashield_backfill(transform,status) VALUES(%s,'running') "
                "ON CONFLICT(transform) DO NOTHING",
                (TRANSFORM,),
            )
            self._status("running")

    def _batch(self) -> bool:
        """Data changes and cursor advancement commit together or both roll back."""
        with self.connection.transaction():
            self.connection.execute("SET LOCAL lock_timeout='1000ms'")
            self.connection.execute("SET LOCAL statement_timeout='5000ms'")
            state = self._state()
            ids = self.connection.execute(
                "SELECT id FROM public.accounts WHERE id>%s ORDER BY id LIMIT %s FOR UPDATE",
                (state["last_id"], self.options.batch_size),
            ).fetchall()
            if not ids:
                # Stabilize the final verification snapshot against concurrent inserts/writes.
                # This lock is brief but real; this engine makes no downtime guarantee.
                self.connection.execute("LOCK TABLE public.accounts IN SHARE MODE")
                rows, mismatches = self.connection.execute(
                    "SELECT count(*),count(*) FILTER (WHERE email_shadow IS DISTINCT FROM email) "
                    "FROM public.accounts"
                ).fetchone()
                if mismatches:
                    if state["rescans"] - self._rescan_base >= self.options.max_rescans:
                        self._status("failed", "concurrent_changes_rescan_limit")
                    else:
                        self.connection.execute(
                            "UPDATE public.schemashield_backfill SET last_id=0,rescans=rescans+1,"
                            "verified_rows=NULL,updated_at=clock_timestamp() WHERE transform=%s",
                            (TRANSFORM,),
                        )
                    return False
                self.connection.execute(
                    "UPDATE public.schemashield_backfill SET status='completed',verified_rows=%s,"
                    "error_code=NULL,updated_at=clock_timestamp() WHERE transform=%s",
                    (rows, TRANSFORM),
                )
                return False
            changed = self.connection.execute(
                "UPDATE public.accounts SET email_shadow=email WHERE id=ANY(%s) "
                "AND email_shadow IS DISTINCT FROM email RETURNING id",
                ([row[0] for row in ids],),
            ).fetchall()
            self.connection.execute(
                "UPDATE public.schemashield_backfill SET last_id=%s,row_updates=row_updates+%s,"
                "batches=batches+1,verified_rows=NULL,updated_at=clock_timestamp() WHERE transform=%s",
                (ids[-1][0], len(changed), TRANSFORM),
            )
            return True

    def run(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        *,
        max_batches: int | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
            raise ValueError("max_batches must be a positive integer or None")
        acquired = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,)
        ).fetchone()[0]
        if not acquired:
            raise BackfillBusy("Another worker owns this fixture backfill")
        started = time.monotonic()
        performed = 0
        try:
            self._initialize()
            self._rescan_base = self._state()["rescans"]
            while True:
                if cancelled():
                    self._status("cancelled")
                    break
                if time.monotonic() - started >= self.options.max_runtime_seconds:
                    self._status("paused", "runtime_limit")
                    break
                if max_batches is not None and performed >= max_batches:
                    self._status("paused")
                    break
                for attempt in range(self.options.max_retries + 1):
                    # Each attempt is a new bounded transaction. Do not start another
                    # after a previous timeout exhausted the worker's total budget.
                    if attempt and time.monotonic() - started >= self.options.max_runtime_seconds:
                        self._status("paused", "runtime_limit")
                        return self._state()
                    try:
                        was_batch = self._batch()
                        break
                    except psycopg.Error as error:
                        if not retryable(error) or attempt == self.options.max_retries:
                            self._status("failed", error.sqlstate or "database_error")
                            return self._state()
                        if cancelled():
                            self._status("cancelled")
                            return self._state()
                        time.sleep(min(0.025 * (2**attempt), 0.2))
                state = self._state()
                if was_batch:
                    performed += 1
                if on_progress is not None:
                    try:
                        on_progress(state)
                    except Exception:
                        self._status("failed", "progress_callback_failed")
                        return self._state()
                if state["status"] in {"failed", "completed"}:
                    break
            return self._state()
        finally:
            if not self.connection.closed:
                try:
                    self.connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
                except psycopg.Error:
                    # Closing the dedicated worker session also releases its advisory lock.
                    self.connection.close()


def run_backfill_demo(
    runner,
    batch_size: int = 100,
    interrupt_after_batches: int = 2,
    cancelled: Callable[[], bool] = lambda: False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Demonstrate committed interruption and reconnect recovery in one owned sandbox.

    This accepts a server-created SandboxRunner, never client SQL or connection
    configuration. Exiting its environment deliberately destroys durable progress.
    """
    from .sandbox import SandboxCancelled, SandboxUnavailable

    BackfillOptions(batch_size=batch_size)
    if type(interrupt_after_batches) is not int or not 1 <= interrupt_after_batches <= 50:
        raise ValueError("interrupt_after_batches must be an integer between 1 and 50")
    started = time.monotonic()
    paused = None
    resumed = None
    metadata = None

    def publish(state, phase):
        if on_progress is not None:
            on_progress({**state, "phase": phase})

    try:
        with runner.environment(cancelled) as (arguments, admin, metadata):
            with psycopg.connect(**arguments, autocommit=True) as first:
                first.execute("ALTER TABLE public.accounts ADD COLUMN email_shadow text")
                baseline = first.execute(
                    "SELECT count(*),md5(string_agg(id::text || ':' || email || ':' || balance::text, ',' ORDER BY id)) FROM public.accounts"
                ).fetchone()
                # Always pause before completion, including maximum-size batches.
                effective_interrupt = min(
                    interrupt_after_batches, max(1, (baseline[0] - 1) // batch_size)
                )
                tick = time.monotonic()
                paused = BackfillEngine(first, batch_size=batch_size).run(
                    cancelled,
                    max_batches=effective_interrupt,
                    on_progress=lambda state: publish(state, "initial_batches"),
                )
                first_duration_ms = round((time.monotonic() - tick) * 1000, 3)
                first_pid = first.info.backend_pid
            # Closing the first session releases all session state and worker locks.
            if paused["status"] in {"cancelled", "failed"}:
                return {
                    "status": paused["status"],
                    "checks": [],
                    "progress": paused,
                    "environment": metadata,
                    "metrics": {"total_duration_ms": round((time.monotonic() - started) * 1000, 3)},
                    "limitations": paused["limitations"],
                }
            publish(paused, "connection_closed")
            with psycopg.connect(**arguments, autocommit=True) as second:
                second_pid = second.info.backend_pid
                tick = time.monotonic()
                resumed = BackfillEngine(second, batch_size=batch_size).run(
                    cancelled,
                    on_progress=lambda state: publish(state, "resumed_batches"),
                )
                resumed_duration_ms = round((time.monotonic() - tick) * 1000, 3)
                after = second.execute(
                    "SELECT count(*),md5(string_agg(id::text || ':' || email || ':' || balance::text, ',' ORDER BY id)) FROM public.accounts"
                ).fetchone()
                mismatches = second.execute(
                    "SELECT count(*) FROM public.accounts WHERE email_shadow IS DISTINCT FROM email"
                ).fetchone()[0]
            checks = [
                {
                    "name": "durable_interruption",
                    "passed": paused["status"] == "paused" and paused["last_id"] > 0,
                },
                {
                    "name": "fresh_connection_resumed",
                    "passed": first_pid != second_pid and resumed["batches"] > paused["batches"],
                },
                {
                    "name": "fixture_source_data_preserved",
                    "passed": baseline == after,
                    "before_count": baseline[0],
                    "after_count": after[0],
                },
                {
                    "name": "every_shadow_value_verified",
                    "passed": resumed["status"] == "completed" and mismatches == 0,
                    "mismatches": mismatches,
                },
                {
                    "name": "no_duplicate_row_updates_in_fixed_fixture",
                    "passed": resumed["row_updates"] == baseline[0],
                },
            ]
            return {
                "status": "cancelled"
                if resumed["status"] == "cancelled"
                else "passed"
                if all(check["passed"] for check in checks)
                else "failed",
                "checks": checks,
                "progress": resumed,
                "environment": metadata,
                "resume_evidence": {
                    "interrupted": paused,
                    "completed": resumed,
                    "original_backend_pid": first_pid,
                    "resumed_backend_pid": second_pid,
                    "requested_interrupt_batches": interrupt_after_batches,
                    "effective_interrupt_batches": effective_interrupt,
                },
                "metrics": {
                    "batch_size": batch_size,
                    "batches": resumed["batches"],
                    "row_updates": resumed["row_updates"],
                    "verified_rows": resumed["verified_rows"],
                    "initial_duration_ms": first_duration_ms,
                    "resume_duration_ms": resumed_duration_ms,
                    "total_duration_ms": round((time.monotonic() - started) * 1000, 3),
                },
                "limitations": resumed["limitations"],
            }
    except (psycopg.Error, SandboxUnavailable, SandboxCancelled) as error:
        return {
            "status": "cancelled" if isinstance(error, SandboxCancelled) else "failed",
            "checks": [],
            "progress": resumed or paused,
            "environment": metadata,
            "error": f"{type(error).__name__}: disposable backfill failed; cleanup attempted",
            "sqlstate": getattr(error, "sqlstate", None),
            "metrics": {"total_duration_ms": round((time.monotonic() - started) * 1000, 3)},
            "limitations": [
                "Reconnect recovery requires the same still-existing disposable database"
            ],
        }
