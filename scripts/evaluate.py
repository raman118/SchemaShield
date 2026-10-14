"""Labeled static detection and timing benchmark; no runtime SQL measurements."""

import argparse
import hashlib
import json
import platform
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from schemashield.policy import analyze

ROOT = Path(__file__).resolve().parents[1]


def evaluate(repetitions: int = 50) -> dict:
    raw = (ROOT / "fixtures/evaluation.json").read_bytes()
    dataset = json.loads(raw)
    counts = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    rules = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    results, timings = [], []
    for case in dataset:
        outputs = []
        for _ in range(repetitions):
            started = time.perf_counter_ns()
            analysis = analyze(case["sql"])
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
            outputs.append(analysis.model_dump_json())
        detected = analysis.decision == "blocked"
        key = (
            "tp"
            if case["unsafe"] and detected
            else "fn"
            if case["unsafe"]
            else "fp"
            if detected
            else "tn"
        )
        counts[key] += 1
        actual = {finding.rule_id for finding in analysis.findings}
        expected = set(case["expected_rules"])
        for rule in actual | expected:
            rules[rule][
                "tp" if rule in actual & expected else "fp" if rule in actual else "fn"
            ] += 1
        results.append(
            {
                "id": case["id"],
                "expected_unsafe": case["unsafe"],
                "detected_unsafe": detected,
                "correct": detected == case["unsafe"],
                "deterministic": len(set(outputs)) == 1,
                "artifact_hash": analysis.artifact_hash,
                "score": analysis.score.value,
                "expected_rules": sorted(expected),
                "actual_rules": sorted(actual),
            }
        )
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "static_policy_only",
        "dataset_hash": hashlib.sha256(raw).hexdigest(),
        "sample_size": len(dataset),
        "repetitions_per_case": repetitions,
        "total_analyses": len(timings),
        "conditions": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "policy": analysis.policy_version,
            "parser": analysis.parsed.parser_version,
        },
        "confusion_matrix": counts,
        "unsafe_detection_recall": counts["tp"] / max(1, counts["tp"] + counts["fn"]),
        "false_positive_rate": counts["fp"] / max(1, counts["fp"] + counts["tn"]),
        "per_rule": {
            rule: {**data, "precision": data["tp"] / max(1, data["tp"] + data["fp"])}
            for rule, data in sorted(rules.items())
        },
        "analysis_latency_ms": {
            "mean": statistics.mean(timings),
            "median": statistics.median(timings),
            "p95": sorted(timings)[int(len(timings) * 0.95) - 1],
            "max": max(timings),
        },
        "cases": results,
        "runtime_results": None,
        "limitations": [
            "24 hand-labeled cases are a small development corpus, not independent production validation",
            "Safe label means no high/critical finding under documented assumptions, not authorization",
            "Per-rule precision uses the explicit hand-reviewed annotations for this corpus only",
            "Measured analysis timing excludes database/network/UI; not migration execution latency",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=50)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/static-evaluation.json")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 1000:
        parser.error("Repetitions must be 1..1000")
    result = evaluate(args.repetitions)
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "sample_size",
                    "confusion_matrix",
                    "unsafe_detection_recall",
                    "false_positive_rate",
                    "analysis_latency_ms",
                )
            }
        )
    )
    return 0 if all(c["correct"] and c["deterministic"] for c in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
