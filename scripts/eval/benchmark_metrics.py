"""Shared metric helpers for benchmark artifacts."""

from __future__ import annotations

import math
from typing import Any


def wilson_interval(
    numerator: int | float,
    denominator: int | float,
    *,
    confidence: float = 0.95,
) -> dict[str, float | int]:
    """Return a Wilson score interval for a binomial proportion."""
    total = int(denominator)
    successes = int(numerator)
    if total <= 0:
        return {"confidence": confidence, "lower": 0.0, "upper": 0.0}
    successes = max(0, min(successes, total))
    z = _z_score(confidence)
    proportion = successes / total
    denominator_term = 1 + (z * z / total)
    center = (proportion + (z * z / (2 * total))) / denominator_term
    margin = (
        z
        * math.sqrt((proportion * (1 - proportion) / total) + (z * z / (4 * total * total)))
        / denominator_term
    )
    return {
        "confidence": confidence,
        "lower": round(max(0.0, center - margin), 4),
        "upper": round(min(1.0, center + margin), 4),
    }


def proportion_metric(
    *,
    numerator: int,
    denominator: int,
    label: str,
    source: str,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Build one auditable proportion metric with counts and a confidence interval."""
    value = round(numerator / denominator, 4) if denominator else 0.0
    return {
        "label": label,
        "value": value,
        "numerator": int(numerator),
        "denominator": int(denominator),
        "sample_count": int(denominator),
        "confidence_interval": wilson_interval(
            numerator,
            denominator,
            confidence=confidence,
        ),
        "source": source,
    }


def _z_score(confidence: float) -> float:
    if confidence >= 0.999:
        return 3.2905
    if confidence >= 0.99:
        return 2.5758
    if confidence >= 0.95:
        return 1.96
    if confidence >= 0.90:
        return 1.6449
    return 1.0
