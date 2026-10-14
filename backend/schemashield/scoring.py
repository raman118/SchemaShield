"""Versioned explainable deductions: 100 means safer, never proven safe."""

from .contracts import Contribution, Finding, SafetyScore

SEVERITY = {"info": 0, "low": 4, "medium": 10, "high": 20, "critical": 35}
CATEGORY_CAPS = {
    "unsupported": 60,
    "data_loss": 35,
    "lock": 25,
    "rewrite": 20,
    "compatibility": 20,
    "reversibility": 15,
    "constraint": 15,
    "operational": 15,
}


def calculate_score(findings: list[Finding], *, tested: bool = False) -> SafetyScore:
    """Maximum finding deduction per category avoids double counting aliases.

    Category order is policy-defined; deductions saturate at zero. The sum of
    emitted deductions is exactly 100 minus the score. Execution evidence can
    only remove its own deduction, never cancel destructive findings.
    """
    contributions = []
    remaining = 100
    if not tested:
        contributions.append(
            Contribution(
                category="verification",
                deduction=20,
                reason="No isolated PostgreSQL runtime verification evidence; static analysis cannot establish operational safety.",
            )
        )
        remaining -= 20
    for category, cap in CATEGORY_CAPS.items():
        relevant = [finding for finding in findings if finding.category == category]
        requested = min(cap, max((SEVERITY[f.severity] for f in relevant), default=0))
        deduction = min(remaining, requested)
        if requested:
            contributions.append(
                Contribution(
                    category=category,
                    deduction=deduction,
                    reason="Highest severity finding in category; cap "
                    + str(cap)
                    + "; rules: "
                    + ", ".join(sorted({f.rule_id for f in relevant})),
                )
            )
            remaining -= deduction
    return SafetyScore(
        value=remaining,
        contributions=contributions,
        explanation="Higher values indicate greater assessed safety. Start at 100; deduct missing evidence and the highest severity per risk category, subject to published category caps and a floor of zero. A score is not execution authorization.",
    )
