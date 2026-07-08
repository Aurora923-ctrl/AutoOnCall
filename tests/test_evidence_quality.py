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


def test_evidence_sufficiency_requires_domain_symptom_and_reference() -> None:
    profile = build_evidence_quality_profile(
        [
            _evidence("redis_info", data_source="redis_info", evidence_type="redis"),
            _evidence("prometheus", data_source="prometheus", evidence_type="metric"),
        ]
    )

    sufficiency = profile["sufficiency"]
    assert sufficiency["complete"] is False
    assert sufficiency["status"] == "degraded"
    assert sufficiency["has_primary_domain_evidence"] is True
    assert sufficiency["has_symptom_evidence"] is True
    assert sufficiency["has_reference_evidence"] is False
    assert "处置参考（Runbook 或历史工单）" in sufficiency["missing_evidence"]


def test_evidence_sufficiency_complete_with_history_ticket() -> None:
    profile = build_evidence_quality_profile(
        [
            _evidence("mysql", data_source="mysql", evidence_type="mysql"),
            _evidence("loki", data_source="loki", evidence_type="log"),
            _evidence("ticket_api", data_source="ticket_api", evidence_type="ticket"),
        ]
    )

    assert profile["sufficiency"]["complete"] is True
    assert profile["sufficiency"]["status"] == "complete"
    assert profile["by_layer"]["live"] == 2
    assert profile["by_layer"]["history"] == 1
    assert profile["root_cause_closure"]["status"] == "satisfied"
    assert profile["root_cause_closure"]["has_live_evidence"] is True
    assert profile["root_cause_closure"]["has_knowledge_or_history"] is True


def test_evidence_profile_tracks_artifacts_and_incomplete_root_cause_closure() -> None:
    profile = build_evidence_quality_profile(
        [
            {
                **_evidence("redis_info", data_source="redis_info", evidence_type="redis"),
                "evidence_id": "evd-live",
                "artifact_refs": [
                    {
                        "artifact_id": "toolout-query-redis-1",
                        "artifact_ref": "data/aiops_tool_artifacts/toolout-query-redis-1.json",
                    }
                ],
            }
        ]
    )

    assert profile["artifact_count"] == 1
    assert profile["by_layer"]["live"] == 1
    assert profile["root_cause_closure"]["status"] == "incomplete"
    assert profile["root_cause_closure"]["missing"] == ["knowledge/history basis"]


def test_runbook_no_answer_rejection_does_not_count_as_reference() -> None:
    profile = build_evidence_quality_profile(
        [
            _evidence("prometheus", data_source="prometheus", evidence_type="metric"),
            _evidence("loki", data_source="loki", evidence_type="log"),
            {
                "source_tool": "search_runbook",
                "evidence_type": "runbook",
                "data_source": "eval_fixture",
                "stance": "supporting",
                "raw_data": {
                    "status": "success",
                    "output": {"no_answer_rejected": True, "summary": "no trusted runbook"},
                },
            },
        ]
    )

    sufficiency = profile["sufficiency"]
    assert sufficiency["complete"] is False
    assert sufficiency["has_reference_evidence"] is False
    assert "可信 Runbook / 历史工单处置参考" in sufficiency["missing_evidence"]
    assert profile["root_cause_closure"]["has_knowledge_or_history"] is False


def test_failed_k8s_domain_tool_blocks_metric_only_primary_substitution() -> None:
    profile = build_evidence_quality_profile(
        [
            {
                "source_tool": "query_k8s_status",
                "evidence_type": "k8s",
                "data_source": "unknown",
                "stance": "neutral",
                "raw_data": {"status": "failed", "error_message": "RBAC denied"},
            },
            _evidence("prometheus", data_source="prometheus", evidence_type="metric"),
            _evidence("loki", data_source="loki", evidence_type="log"),
            _evidence("ticket_api", data_source="ticket_api", evidence_type="ticket"),
        ]
    )

    sufficiency = profile["sufficiency"]
    assert sufficiency["complete"] is False
    assert sufficiency["has_primary_domain_evidence"] is False
    assert "query_k8s_status" in sufficiency["failed_tools"]
