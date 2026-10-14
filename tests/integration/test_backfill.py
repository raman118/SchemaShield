import psycopg
import pytest
from schemashield.backfill import ADVISORY_LOCK, BackfillBusy, BackfillEngine
from schemashield.config import Settings
from schemashield.sandbox import SandboxRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox():
    runner = SandboxRunner(
        Settings(sandbox_enabled=True, api_token="real-integration-control-token-32")
    )
    capability = runner.capability()
    if not capability["sandbox_available"]:
        pytest.skip("Real backfill acceptance blocked: " + capability["reason"])
    with runner.environment() as (arguments, admin, metadata):
        with psycopg.connect(**arguments, autocommit=True) as connection:
            connection.execute("ALTER TABLE public.accounts ADD COLUMN email_shadow text")
            yield arguments, connection


def test_interrupt_reconnect_resume_and_source_mutation(sandbox):
    arguments, connection = sandbox
    paused = BackfillEngine(connection, batch_size=137).run(max_batches=2)
    assert paused["status"] == "paused"
    assert paused["last_id"] == 274
    assert (
        connection.execute(
            "SELECT count(*) FROM accounts WHERE email_shadow IS NOT NULL"
        ).fetchone()[0]
        == 274
    )
    # Simulate application update of a row already processed by the interrupted worker.
    with psycopg.connect(**arguments, autocommit=True) as writer:
        writer.execute("UPDATE accounts SET email='concurrently-changed@example.test' WHERE id=1")
    with psycopg.connect(**arguments, autocommit=True) as resumed:
        result = BackfillEngine(resumed, batch_size=97).run()
    assert result["status"] == "completed"
    assert result["rescans"] == 1
    assert result["verified_rows"] == 2000
    assert (
        connection.execute(
            "SELECT count(*) FROM accounts WHERE email_shadow IS DISTINCT FROM email"
        ).fetchone()[0]
        == 0
    )
    # Completed status must be reverified on another run, never trusted indefinitely.
    connection.execute("UPDATE accounts SET email='changed-again@example.test' WHERE id=2")
    again = BackfillEngine(connection).run()
    assert again["status"] == "completed"
    assert again["rescans"] == 2


def test_advisory_worker_exclusion_and_cancel_resume(sandbox):
    arguments, connection = sandbox
    with psycopg.connect(**arguments, autocommit=True) as competing:
        competing.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK,))
        with pytest.raises(BackfillBusy):
            BackfillEngine(connection).run()
        competing.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
    cancelled = BackfillEngine(connection).run(cancelled=lambda: True)
    assert cancelled["status"] == "cancelled"
    assert cancelled["batches"] == 0
    completed = BackfillEngine(connection).run()
    assert completed["status"] == "completed"
    assert completed["row_updates"] == 2000


def test_failed_progress_write_rolls_back_the_same_data_batch(sandbox):
    _, connection = sandbox
    BackfillEngine(connection, batch_size=100).run(max_batches=1)
    connection.execute(
        "ALTER TABLE schemashield_backfill ADD CONSTRAINT stop_progress CHECK (last_id<=100)"
    )
    failed = BackfillEngine(connection, batch_size=100).run()
    assert failed["status"] == "failed"
    assert failed["last_id"] == 100
    assert failed["batches"] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM accounts WHERE email_shadow IS NOT NULL"
        ).fetchone()[0]
        == 100
    )
    connection.execute("ALTER TABLE schemashield_backfill DROP CONSTRAINT stop_progress")
    assert BackfillEngine(connection).run()["status"] == "completed"


def test_completed_resume_clears_stale_verification_and_remains_idempotent(sandbox):
    _, connection = sandbox
    completed = BackfillEngine(connection).run()
    assert completed["verified_rows"] == 2000
    paused = BackfillEngine(connection).run(cancelled=lambda: True)
    assert paused["status"] == "cancelled"
    assert paused["verified_rows"] is None
    assert paused["verification"]["performed"] is False
    repeated = BackfillEngine(connection).run()
    assert repeated["status"] == "completed"
    assert repeated["row_updates"] == completed["row_updates"]


def test_rescan_limit_fails_closed_then_can_resume_for_null_source_values(sandbox):
    _, connection = sandbox
    BackfillEngine(connection, batch_size=100).run(max_batches=1)
    connection.execute("ALTER TABLE accounts ALTER COLUMN email DROP NOT NULL")
    connection.execute("UPDATE accounts SET email=NULL WHERE id=1")
    failed = BackfillEngine(connection, max_rescans=0).run()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "concurrent_changes_rescan_limit"
    assert failed["verified_rows"] is None
    recovered = BackfillEngine(connection).run()
    assert recovered["status"] == "completed"
    assert connection.execute("SELECT email_shadow FROM accounts WHERE id=1").fetchone() == (None,)
