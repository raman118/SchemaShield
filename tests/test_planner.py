import pytest
from pglast import parse_sql
from schemashield.planner import generate_plan
from schemashield.policy import analyze


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t(id int)",
        "ALTER TABLE t ADD COLUMN x text",
        "CREATE INDEX ix ON t(x)",
        "CREATE UNIQUE INDEX ix ON t(x) WHERE x>0",
        "ALTER TABLE t ADD CONSTRAINT ck CHECK(x>0)",
        "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY(x) REFERENCES p(id)",
        'ALTER TABLE "ü" ALTER COLUMN "名" TYPE bigint',
        "ALTER TABLE t ALTER COLUMN x SET NOT NULL",
        "ALTER TABLE t VALIDATE CONSTRAINT ck",
        "ALTER TABLE t ADD COLUMN a text, ADD COLUMN b bigint",
    ],
)
def test_generated_sql_is_postgresql_parseable(sql):
    analysis = analyze(sql)
    plan = generate_plan(analysis)
    assert plan.eligible, plan.reason
    assert plan.artifact_hash == analysis.artifact_hash
    assert plan.steps
    for step in plan.steps:
        assert step.preconditions
        if step.sql:
            assert parse_sql(step.sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DROP INDEX ix",
        "ALTER TABLE t DROP COLUMN x",
        "ALTER TABLE t ADD COLUMN x int NOT NULL",
        "SELECT 1",
        "CREATE INDEX ON t(x)",
        "ALTER TABLE t ADD CHECK(x>0)",
        "CREATE TABLE IF NOT EXISTS t(x int)",
        "ALTER TABLE t ADD CONSTRAINT uq UNIQUE(x)",
    ],
)
def test_unsupported_transformations_reject(sql):
    plan = generate_plan(analyze(sql))
    assert not plan.eligible
    assert plan.reason
    assert not plan.steps


def test_concurrent_index_requires_autocommit():
    plan = generate_plan(analyze("CREATE INDEX ix ON t(x)"))
    assert "CONCURRENTLY" in plan.steps[0].sql
    assert plan.steps[0].transaction == "autocommit"
    assert any("indisvalid" in (step.sql or "") for step in plan.steps)


def test_constraints_use_not_valid_then_validate():
    plan = generate_plan(analyze("ALTER TABLE t ADD CONSTRAINT ck CHECK(x>0)"))
    assert "NOT VALID" in plan.steps[0].sql
    assert "VALIDATE CONSTRAINT" in plan.steps[1].sql


def test_type_mapping_cannot_be_invented():
    plan = generate_plan(analyze("ALTER TABLE t ALTER COLUMN x TYPE bigint USING x::bigint"))
    assert {s.phase for s in plan.steps} == {"EXPAND", "BACKFILL", "VERIFY", "CONTRACT"}
    assert any(
        s.phase == "BACKFILL" and s.transaction == "manual" and s.sql is None for s in plan.steps
    )
    contract = [s for s in plan.steps if s.phase == "CONTRACT"]
    assert all(s.requires_approval for s in contract)
    assert contract[-1].transaction == "manual"


def test_multi_alter_does_not_duplicate_full_source():
    plan = generate_plan(analyze("ALTER TABLE t ADD COLUMN a text, ADD COLUMN b int"))
    assert len(plan.steps) == 2
    assert '"a"' in plan.steps[0].sql
    assert '"b"' not in plan.steps[0].sql


def test_identifier_injection_is_quoted():
    plan = generate_plan(analyze('ALTER TABLE "t; DROP TABLE p;" ADD COLUMN "x""y" int'))
    assert plan.eligible
    assert len(parse_sql(plan.steps[0].sql)) == 1
