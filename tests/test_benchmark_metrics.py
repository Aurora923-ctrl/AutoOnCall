"""Tests for benchmark metric contracts."""

from scripts.eval.benchmark_metrics import proportion_metric, wilson_interval


def test_wilson_interval_handles_small_perfect_samples() -> None:
    interval = wilson_interval(20, 20)

    assert interval["lower"] < 1.0
    assert interval["upper"] == 1.0


def test_proportion_metric_keeps_raw_counts() -> None:
    metric = proportion_metric(
        numerator=18,
        denominator=20,
        label="parse success",
        source="assets",
    )

    assert metric["value"] == 0.9
    assert metric["numerator"] == 18
    assert metric["denominator"] == 20
    assert metric["sample_count"] == 20
