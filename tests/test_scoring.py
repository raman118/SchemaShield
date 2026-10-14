import pytest
from schemashield.policy import analyze
from schemashield.scoring import calculate_score


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t(id int)",
        "DROP TABLE t CASCADE",
        "SELECT 1",
        "ALTER TABLE t ALTER COLUMN x TYPE int",
        "CREATE UNIQUE INDEX ix ON t(x)",
    ],
)
def test_score_contributions_reconcile(sql):
    score = analyze(sql).score
    assert 100 - sum(c.deduction for c in score.contributions) == score.value
    assert score.higher_is_safer
    assert score.policy_version == "score-1.0"


def test_missing_evidence_deduction_is_explicit():
    assert calculate_score([]).value == 80
    assert calculate_score([], tested=True).value == 100
    assert calculate_score([]).contributions[0].category == "verification"


def test_repeating_findings_cannot_arbitrarily_amplify_score():
    findings = analyze("DROP TABLE t").findings
    assert calculate_score(findings).value == calculate_score(findings * 10).value


def test_runtime_evidence_does_not_clear_destructive_risk():
    findings = analyze("DROP TABLE t CASCADE").findings
    assert calculate_score(findings, tested=True).value < 50


def test_safe_addition_scores_higher_than_destructive_drop():
    assert (
        analyze("ALTER TABLE t ADD COLUMN x text").score.value
        > analyze("ALTER TABLE t DROP COLUMN x").score.value
    )
