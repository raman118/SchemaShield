"""Reproducible empirical PostgreSQL fixture evaluation; unavailable infrastructure fails."""

import argparse
import json
import statistics
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from schemashield.backfill import run_backfill_demo
from schemashield.config import get_settings
from schemashield.sandbox import SandboxRunner

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = [
    ("nullable_add", ["ALTER TABLE accounts ADD COLUMN nickname text"], "passed"),
    ("concurrent_index", ["CREATE INDEX CONCURRENTLY email_idx ON accounts(email)"], "passed"),
    ("new_table", ["CREATE TABLE notes (id bigint PRIMARY KEY, note text)"], "passed"),
    (
        "staged_check",
        [
            "ALTER TABLE accounts ADD CONSTRAINT balance_ok CHECK(balance>=0) NOT VALID",
            "ALTER TABLE accounts VALIDATE CONSTRAINT balance_ok",
        ],
        "passed",
    ),
    (
        "staged_fk",
        [
            "ALTER TABLE orders ADD CONSTRAINT accounts_fk FOREIGN KEY(account_id) REFERENCES accounts(id) NOT VALID",
            "ALTER TABLE orders VALIDATE CONSTRAINT accounts_fk",
        ],
        "passed",
    ),
    (
        "partial_failure",
        [
            "ALTER TABLE accounts ADD COLUMN duplicate text",
            "ALTER TABLE accounts ADD COLUMN duplicate text",
        ],
        "failed",
    ),
]


def scenario_correct(name: str, expected: str, report: dict) -> bool:
    if report.get("status") != expected:
        return False
    if name == "partial_failure":
        # An infrastructure or authorization failure is not evidence that the
        # second statement failed after the first one committed.
        return (
            report.get("sqlstate") == "42701"
            and len(report.get("metrics", {}).get("statement_timings_ms", [])) == 1
        )
    return True


def contention(runner: SandboxRunner) -> dict:
    with runner.environment() as (args, admin, environment):
        outcome = {}
        started = time.perf_counter()
        with psycopg.connect(**args) as blocker:
            blocker.execute("SELECT * FROM accounts WHERE id=1").fetchone()

            def migrate():
                tick = time.perf_counter()
                try:
                    with psycopg.connect(
                        **args, autocommit=True, application_name="schemashield-contention"
                    ) as connection:
                        connection.execute("SET lock_timeout='700ms'")
                        connection.execute("ALTER TABLE accounts ADD COLUMN waiting_column text")
                    outcome["status"] = "unexpected_success"
                except psycopg.Error as exc:
                    outcome["status"] = (
                        "expected_lock_timeout" if exc.sqlstate == "55P03" else "failed"
                    )
                    outcome["sqlstate"] = exc.sqlstate
                except Exception as exc:
                    outcome["status"] = "failed"
                    outcome["error"] = type(exc).__name__
                outcome["duration_ms"] = (time.perf_counter() - tick) * 1000

            thread = threading.Thread(target=migrate, daemon=True)
            thread.start()
            samples = []
            while thread.is_alive() and time.perf_counter() - started < 3:
                rows = admin.execute(
                    "SELECT pid, wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE application_name='schemashield-contention'"
                ).fetchall()
                for pid, wait_type, blockers in rows:
                    samples.append(
                        {
                            "elapsed_ms": (time.perf_counter() - started) * 1000,
                            "wait_event_type": wait_type,
                            "blocking_pids": blockers,
                            "locks": [
                                {"mode": mode, "granted": granted}
                                for mode, granted in admin.execute(
                                    "SELECT mode,granted FROM pg_locks WHERE pid=%s", (pid,)
                                ).fetchall()
                            ],
                        }
                    )
                time.sleep(0.025)
            blocker.rollback()
            thread.join(timeout=6)
        outcome["samples"] = samples
        outcome["observed_wait_duration_ms"] = (
            sum(s["wait_event_type"] == "Lock" for s in samples) * 25
        )
        outcome["blocking_observed"] = any(s["blocking_pids"] for s in samples)
        outcome["environment"] = environment
        return outcome


def evaluate(repetitions: int = 3):
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("Repetitions must be an integer between 1 and 10")
    settings = get_settings()
    runner = SandboxRunner(settings)
    capability = runner.capability()
    base = {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "empirical_owned_postgresql_fixture",
        "capability": capability,
        "image": settings.sandbox_image,
        "repetitions": repetitions,
        "fixture_rows": 2000,
        "cpu_limit": 1,
        "memory_limit_mb": 384,
        "postgres_data_tmpfs_mb": 192,
        "statement_timeout_ms": 5000,
        "lock_timeout_ms": 1000,
        "sample_interval_ms": 25,
        "planned_samples": len(SCENARIOS) * repetitions,
        "planned_scenarios": [name for name, _, _ in SCENARIOS],
    }
    if not capability["sandbox_available"]:
        return {
            **base,
            "status": "blocked",
            "cases": [],
            "completed_samples": 0,
            "skipped_samples": len(SCENARIOS) * repetitions,
            "metrics": None,
            "error": "Actual Docker/PostgreSQL required; no measurements fabricated",
        }
    cases = []
    try:
        for name, statements, expected in SCENARIOS:
            for repeat in range(repetitions):
                report = runner.run(statements)
                cases.append(
                    {
                        "scenario": name,
                        "repeat": repeat + 1,
                        "expected": expected,
                        "correct": scenario_correct(name, expected, report),
                        "report": report,
                    }
                )
        backfill = run_backfill_demo(runner, batch_size=100, interrupt_after_batches=2)
        locking = contention(runner)
        eligible = [case for case in cases if case["expected"] == "passed"]
        timings = [
            case["report"]["metrics"]["migration_duration_ms"]
            for case in eligible
            if case["report"]["status"] == "passed"
        ]
        queries = sum(case["report"]["metrics"]["application_queries"] for case in cases)
        errors = sum(case["report"]["metrics"]["application_errors"] for case in cases)
        return {
            **base,
            "status": "passed"
            if all(case["correct"] for case in cases)
            and backfill["status"] == "passed"
            and locking.get("blocking_observed")
            and locking.get("sqlstate") == "55P03"
            else "failed",
            "cases": cases,
            "completed_samples": len(cases),
            "skipped_samples": 0,
            "backfill": backfill,
            "contention": locking,
            "metrics": {
                "successful_eligible_completion_rate": sum(
                    case["report"]["status"] == "passed" for case in eligible
                )
                / len(eligible),
                "migration_latency_ms": {
                    "sample_size": len(timings),
                    "mean": statistics.mean(timings) if timings else None,
                    "median": statistics.median(timings) if timings else None,
                    "max": max(timings) if timings else None,
                    "scope": "Successful eligible migrations only",
                },
                "controlled_application_queries": queries,
                "controlled_application_errors": errors,
                "application_error_rate": errors / queries if queries else None,
                "backfill_recovery_success": backfill["status"] == "passed",
                "contention_observed_wait_ms": locking["observed_wait_duration_ms"],
            },
            "limitations": [
                "Small fixed fixtures and local WSL/Linux host; no production representativeness claim",
                "Failure case intentionally autocommits its first statement; cleanup is disposal, not rollback",
                "Lock wait duration is sampled with 25ms resolution",
                "Backfill reconnect recovery occurs within one owned environment lease; destruction removes progress",
                "Application workload is one fixed read/update loop, not an application compatibility proof",
            ],
        }
    except Exception as exc:
        return {
            **base,
            "status": "failed",
            "cases": cases,
            "completed_samples": len(cases),
            "skipped_samples": len(SCENARIOS) * repetitions - len(cases),
            "metrics": None,
            "error": f"{type(exc).__name__}: runtime evaluation could not complete",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/runtime-evaluation.json")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10:
        parser.error("Repetitions must be 1..10")
    report = evaluate(args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "case_count": len(report["cases"]),
                "metrics": report["metrics"],
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
