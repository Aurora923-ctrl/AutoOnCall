"""Tests for shared evidence source quality rules."""

from app.services.evidence_quality import (
    build_evidence_quality_profile,
    evidence_data_source,
    source_quality_confidence_cap,
)


def _evidence(source: str, *, data_source: str = "", evidence_type: str = "metric") -> dict:
    return {
        "source_tool": "query_metrics",
        "evidence_type": evidence_type,
        "data_source": data_source,
        "stance": "supporting",
        "confidence": 0.9,
        "raw_data": {
            "status": "success",
            "output": {"source": source, "summary": "ok"},
        },
    }


def test_evidence_quality_profile_classifies_mixed_sources() -> None:
    profile = build_evidence_quality_profile(
        [
            _evidence("prometheus"),
            _evidence("mock"),
            _evidence("", data_source="unknown"),
        ]
    )

    assert profile["source_quality"] == "mixed_with_fallback"
    assert profile["trusted_source_count"] == 1
    assert profile["fallback_source_count"] == 1
    assert profile["degraded_source_count"] == 1


def test_evidence_quality_profile_classifies_fallback_only_sources() -> None:
    profile = build_evidence_quality_profile(
        [
            _evidence("mock"),
            _evidence("rule_based", evidence_type="log"),
        ]
    )

    assert profile["source_quality"] == "fallback_only"
    assert source_quality_confidence_cap([], {"evidence_profile": profile}) == 0.5


def test_evidence_data_source_normalizes_raw_output_when_label_is_unknown() -> None:
    item = _evidence("prometheus", data_source="unknown")

    assert evidence_data_source(item) == "prometheus"
    assert source_quality_confidence_cap([item], {}) is None
