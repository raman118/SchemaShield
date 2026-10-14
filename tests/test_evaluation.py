from scripts.evaluate import evaluate


def test_labeled_corpus_is_deterministic_and_reports_counts():
    report = evaluate(2)
    assert sum(report["confusion_matrix"].values()) == report["sample_size"]
    assert all(case["deterministic"] for case in report["cases"])
    assert report["runtime_results"] is None
    assert report["analysis_latency_ms"]["mean"] > 0
