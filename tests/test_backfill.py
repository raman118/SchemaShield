from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from schemashield.backfill import (
    BackfillBusy,
    BackfillEngine,
    BackfillError,
    BackfillOptions,
    retryable,
    run_backfill_demo,
)


@pytest.mark.parametrize(
    "name,value",
    [
        ("batch_size", 0),
        ("batch_size", 1001),
        ("batch_size", True),
        ("batch_size", "10"),
        ("max_retries", -1),
        ("max_retries", 6),
        ("max_rescans", 11),
        ("max_runtime_seconds", 0),
        ("max_runtime_seconds", 181),
    ],
)
def test_resource_bounds_fail_closed(name, value):
    with pytest.raises(ValueError, match=name):
        BackfillOptions(**{name: value})


def test_options_accept_bounded_adjustment():
    assert BackfillOptions(batch_size=1000, max_retries=0).batch_size == 1000


@pytest.mark.parametrize(
    "autocommit,status",
    [
        (False, TransactionStatus.IDLE),
        (True, TransactionStatus.INTRANS),
        (True, TransactionStatus.INERROR),
    ],
)
def test_existing_transactions_cannot_hide_progress_commit(autocommit, status):
    connection = SimpleNamespace(
        autocommit=autocommit, info=SimpleNamespace(transaction_status=status)
    )
    with pytest.raises(BackfillError, match="idle autocommit"):
        BackfillEngine(connection)


def test_second_worker_rejected_before_schema_or_data_changes():
    statements = []

    def execute(statement, parameters):
        statements.append(statement)
        return SimpleNamespace(fetchone=lambda: (False,))

    connection = SimpleNamespace(
        autocommit=True,
        info=SimpleNamespace(transaction_status=TransactionStatus.IDLE),
        execute=execute,
    )
    with pytest.raises(BackfillBusy):
        BackfillEngine(connection).run()
    assert statements == ["SELECT pg_try_advisory_lock(%s)"]


@pytest.mark.parametrize(
    "error,expected",
    [
        (psycopg.errors.SerializationFailure(), True),
        (psycopg.errors.DeadlockDetected(), True),
        (psycopg.errors.LockNotAvailable(), True),
        (psycopg.errors.UndefinedColumn(), False),
        (psycopg.errors.InsufficientPrivilege(), False),
    ],
)
def test_retry_policy_never_retries_permanent_schema_or_auth_failures(error, expected):
    assert retryable(error) is expected


@pytest.mark.parametrize("max_batches", [0, -1, True, "4"])
def test_interruption_bounds_validate_before_database_access(max_batches):
    connection = SimpleNamespace(
        autocommit=True, info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)
    )
    with pytest.raises(ValueError, match="max_batches"):
        BackfillEngine(connection).run(max_batches=max_batches)


class ControlledEngine(BackfillEngine):
    """Exercise worker control behavior without pretending to execute PostgreSQL."""

    def __init__(self, failure=None, max_retries=3):
        self.statements = []

        def execute(statement, parameters):
            self.statements.append(statement)
            return SimpleNamespace(fetchone=lambda: (True,))

        connection = SimpleNamespace(
            autocommit=True,
            closed=False,
            info=SimpleNamespace(transaction_status=TransactionStatus.IDLE),
            execute=execute,
        )
        super().__init__(connection, max_retries=max_retries)
        self.progress = {"status": "running", "rescans": 0}
        self.failure = failure
        self.attempts = 0

    def _initialize(self):
        pass

    def _state(self):
        return self.progress.copy()

    def _status(self, status, error_code=None):
        self.progress.update(status=status, error_code=error_code)

    def _batch(self):
        self.attempts += 1
        if self.failure is not None:
            raise self.failure
        self._status("completed")
        return False


def test_retry_exhaustion_records_failure_and_releases_worker_lock(monkeypatch):
    monkeypatch.setattr("schemashield.backfill.time.sleep", lambda _: None)
    engine = ControlledEngine(psycopg.errors.SerializationFailure(), max_retries=2)
    result = engine.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "40001"
    assert engine.attempts == 3
    assert engine.statements[-1] == "SELECT pg_advisory_unlock(%s)"


def test_permanent_failure_is_not_retried(monkeypatch):
    engine = ControlledEngine(psycopg.errors.InsufficientPrivilege())
    result = engine.run()
    assert result["status"] == "failed"
    assert engine.attempts == 1


def test_cancellation_checkpoint_precedes_any_new_batch():
    engine = ControlledEngine()
    assert engine.run(cancelled=lambda: True)["status"] == "cancelled"
    assert engine.attempts == 0
    assert engine.statements[-1] == "SELECT pg_advisory_unlock(%s)"


def test_failed_progress_observer_is_recorded_and_lock_released():
    def observer(_):
        raise RuntimeError("private callback details must not be persisted")

    engine = ControlledEngine()
    result = engine.run(on_progress=observer)
    assert result["status"] == "failed"
    assert result["error_code"] == "progress_callback_failed"
    assert engine.statements[-1] == "SELECT pg_advisory_unlock(%s)"


def test_runtime_limit_pauses_at_a_durable_batch_boundary(monkeypatch):
    ticks = iter([0, 31])
    monkeypatch.setattr("schemashield.backfill.time.monotonic", lambda: next(ticks))
    engine = ControlledEngine()
    result = engine.run()
    assert result["status"] == "paused"
    assert result["error_code"] == "runtime_limit"
    assert engine.attempts == 0


def test_retry_stops_when_previous_attempt_exhausted_total_runtime(monkeypatch):
    ticks = iter([0, 0, 31])
    monkeypatch.setattr("schemashield.backfill.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("schemashield.backfill.time.sleep", lambda _: None)
    engine = ControlledEngine(psycopg.errors.SerializationFailure())
    result = engine.run()
    assert result["status"] == "paused"
    assert result["error_code"] == "runtime_limit"
    assert engine.attempts == 1
    assert engine.statements[-1] == "SELECT pg_advisory_unlock(%s)"


@pytest.mark.parametrize(
    "sqlstate,timings,correct",
    [
        (None, [], False),
        ("42501", [], False),
        ("42701", [], False),
        ("42701", [0.5, 0.7], False),
        ("42701", [0.5], True),
    ],
)
def test_runtime_failure_scenario_requires_actual_partial_execution(sqlstate, timings, correct):
    from scripts.evaluate_runtime import scenario_correct

    assert (
        scenario_correct(
            "partial_failure",
            "failed",
            {
                "status": "failed",
                "sqlstate": sqlstate,
                "metrics": {"statement_timings_ms": timings},
            },
        )
        is correct
    )


@pytest.mark.parametrize("interrupt", [0, 51, True, "2"])
def test_demo_rejects_unbounded_interrupt_before_creating_environment(interrupt):
    with pytest.raises(ValueError, match="interrupt_after_batches"):
        run_backfill_demo(None, interrupt_after_batches=interrupt)


def test_demo_disabled_sandbox_is_a_real_failure_without_fabricated_checks():
    from schemashield.config import Settings
    from schemashield.sandbox import SandboxRunner

    report = run_backfill_demo(SandboxRunner(Settings(sandbox_enabled=False)))
    assert report["status"] == "failed"
    assert report["checks"] == []
    assert report["environment"] is None
    assert "SandboxUnavailable" in report["error"]
