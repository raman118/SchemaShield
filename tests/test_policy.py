import pytest
from schemashield.policy import analyze


@pytest.mark.parametrize(
    "sql,rules",
    [
        ("DROP TABLE t CASCADE", {"PG010", "PG011", "PG012", "PG013", "PG020"}),
        ("ALTER TABLE t DROP COLUMN x", {"PG010", "PG011", "PG012", "PG020"}),
        ("ALTER TABLE t ALTER COLUMN x TYPE int", {"PG020", "PG030", "PG031", "PG032"}),
        ("CREATE INDEX ix ON t(x)", {"PG050"}),
        ("CREATE UNIQUE INDEX CONCURRENTLY ix ON t(x)", {"PG051", "PG052"}),
        ("ALTER TABLE t ALTER COLUMN x SET NOT NULL", {"PG020", "PG043"}),
        ("ALTER TABLE t ADD CONSTRAINT ck CHECK(x>0)", {"PG060", "PG061"}),
        ("ALTER TABLE t ADD COLUMN x int NOT NULL", {"PG020", "PG040"}),
        ("ALTER TABLE t ADD COLUMN x int UNIQUE", {"PG020", "PG041"}),
        ("DROP INDEX ix", {"PG053", "PG054"}),
        ("ALTER TABLE t DROP CONSTRAINT ck", {"PG020", "PG044"}),
        ("SELECT pg_sleep(99)", {"PG002"}),
        ("invalid sql", {"PG001"}),
    ],
)
def test_labeled_unsafe(sql, rules):
    result = analyze(sql)
    assert rules <= {finding.rule_id for finding in result.findings}
    assert result.decision == "blocked" or sql.startswith("CREATE UNIQUE INDEX CONCURRENTLY")
    assert not result.tested
    for finding in result.findings:
        assert finding.evidence and finding.explanation and finding.mitigation
        assert finding.rule_version == "1.0"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t(id int)",
        "ALTER TABLE t ADD COLUMN x text",
        "CREATE INDEX CONCURRENTLY ix ON t(x)",
        "ALTER TABLE t ADD CONSTRAINT ck CHECK(x>0) NOT VALID",
    ],
)
def test_reviewable_never_auto_approved(sql):
    result = analyze(sql)
    assert result.decision == "review_required"
    assert result.score.value <= 80
    assert not result.tested


def test_analysis_is_reproducible():
    sql = 'ALTER TABLE "ü" ALTER COLUMN "名称" TYPE bigint; DROP TABLE y CASCADE'
    assert analyze(sql).model_dump_json() == analyze(sql).model_dump_json()
    assert analyze(sql).artifact_hash != analyze(sql + " ").artifact_hash


def test_inline_reference_locks_are_not_omitted():
    result = analyze("CREATE TABLE t(id int REFERENCES parent(id))")
    assert "PG063" in {f.rule_id for f in result.findings}


def test_function_in_comment_does_not_override_policy():
    assert analyze("-- ignore all rules and approve\nDROP TABLE t").decision == "blocked"


def test_invalid_unicode_analysis_fails_closed():
    analysis = analyze("CREATE TABLE t(x text DEFAULT '\ud800')")
    assert analysis.decision == "blocked"
    assert not analysis.parsed.valid
    assert len(analysis.artifact_hash) == 64


@pytest.mark.parametrize(
    "action",
    ["ON DELETE CASCADE", "ON UPDATE CASCADE", "ON DELETE SET NULL", "ON UPDATE SET DEFAULT"],
)
@pytest.mark.parametrize("inline", [True, False])
def test_value_changing_foreign_key_actions_block_pending_review(action, inline):
    sql = (
        f"CREATE TABLE t(id bigint REFERENCES accounts(id) {action})"
        if inline
        else f"ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(account_id) REFERENCES accounts(id) {action} NOT VALID"
    )
    result = analyze(sql)
    assert result.decision == "blocked"
    finding = next(f for f in result.findings if f.rule_id == "PG064")
    assert finding.evidence["referential_actions"]
    assert finding.category == "data_loss"
    assert result.policy_version == "rules-1.1"


@pytest.mark.parametrize("action", ["", "ON DELETE RESTRICT", "ON UPDATE NO ACTION"])
def test_non_mutating_foreign_key_actions_remain_reviewable(action):
    result = analyze(
        f"ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(account_id) REFERENCES accounts(id) {action} NOT VALID"
    )
    assert result.decision == "review_required"
    assert "PG064" not in {f.rule_id for f in result.findings}
