"""Tests for structured evidence analysis."""

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import analyze_evidence
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
)
from app.services.diagnostic_signal_rules import (
    evidence_matches_category,
    is_metric_abnormal,
    missing_tools_from_context,
)
from app.tools.base import ToolExecutionResult

DEFAULT_DATA_SOURCE_BY_TOOL = {
    "query_metrics": "prometheus",
    "query_logs": "loki",
    "query_redis_status": "redis_info",
    "query_mysql_status": "mysql",
    "query_k8s_status": "kubernetes",
}


def evidence_from_tool(
    tool_name: str,
    output: dict,
    step_id: str = "s1",
    *,
    status: str = "success",
    data_source: str | None = None,
) -> dict:
    result = ToolExecutionResult(
        tool_name=tool_name,
        status=status,  # type: ignore[arg-type]
        input_args={"service_name": "order-service"},
        output=output,
        error_message=output.get("error_message") if status == "failed" else None,
    )
    raw_data = result.model_dump(mode="json")
    stance = infer_evidence_stance(
        source_tool=tool_name,
        raw_data=raw_data,
        summary=str(output.get("summary", "")),
    )
    return Evidence(
        source_tool=tool_name,
        step_id=step_id,
        summary=str(output.get("summary", "")),
        evidence_type=infer_evidence_type(tool_name),
        data_source=data_source
        or str(output.get("source") or DEFAULT_DATA_SOURCE_BY_TOOL.get(tool_name, "unknown")),
        stance=stance,
        confidence_reason=build_confidence_reason(
            source_tool=tool_name,
            raw_data=raw_data,
            stance=stance,
        ),
        raw_data=raw_data,
        confidence=0.75 if status == "success" else 0.1,
    ).model_dump(mode="json")


def test_analyzer_marks_redis_maxclients_evidence_as_report_ready() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="analysis-redis",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=3250ms, 5xx=8.20%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in /api/order/create"},
            "s2",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients=9940/10000",
                "connected_clients": 9940,
                "maxclients": 10000,
                "client_usage_ratio": 0.994,
                "alert_info": {"triggered": True},
            },
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "generate_report"
    assert analysis.evidence_sufficient is True
    assert analysis.confidence >= 0.8
    assert any("Redis" in item and "maxclients" in item for item in analysis.hypotheses)
    assert analysis.hypothesis_ranking
    assert analysis.hypothesis_ranking[0].category == "redis_maxclients"
    assert analysis.hypothesis_ranking[0].supporting_evidence_ids
    assert analysis.hypothesis_ranking[0].confidence_reason
    assert analysis.missing_evidence == []
    assert analysis.evidence_profile["by_type"]["redis"] == 1
    assert any("阈值" in reason for reason in analysis.confidence_reasons)


def test_redis_golden_incident_waits_for_runbook_and_history_before_report() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="analysis-redis-golden",
    )
    state["incident"].update(
        {
            "service_name": "order-service",
            "raw_alert": {
                "alertname": "RedisMaxClientsNearLimit",
                "dependency": "redis-order",
            },
        }
    )
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=3250ms, 5xx=8.20%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in /api/order/create"},
            "s2",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients=9940/10000",
                "connected_clients": 9940,
                "maxclients": 10000,
                "client_usage_ratio": 0.994,
                "alert_info": {"triggered": True},
            },
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "add_steps"
    assert analysis.evidence_sufficient is False
    assert {"search_runbook", "search_history_ticket"} <= set(analysis.missing_evidence)


def test_analyzer_caps_unknown_successful_evidence_sources() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="analysis-unknown-source",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=3250ms, 5xx=8.20%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
            data_source="unknown",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in /api/order/create"},
            "s2",
            data_source="unknown",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients=9940/10000",
                "connected_clients": 9940,
                "maxclients": 10000,
                "client_usage_ratio": 0.994,
                "alert_info": {"triggered": True},
            },
            "s3",
            data_source="unknown",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "generate_report"
    assert analysis.evidence_sufficient is False
    assert analysis.confidence <= 0.5
    assert analysis.evidence_profile["source_quality"] == "fallback_only"
    assert analysis.evidence_profile["degraded_source_count"] == 3
    assert any("未知来源" in reason for reason in analysis.confidence_reasons)


def test_mock_evidence_cannot_create_root_cause_hypothesis() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="analysis-mock-source",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients=9940/10000",
                "connected_clients": 9940,
                "maxclients": 10000,
                "client_usage_ratio": 0.994,
            },
            data_source="mock",
        )
    ]

    analysis = analyze_evidence(state)

    assert analysis.evidence_sufficient is False
    assert analysis.confidence <= 0.5
    assert all(not item.supporting_evidence_ids for item in analysis.hypothesis_ranking)


def test_analyzer_recommends_missing_redis_evidence_when_plan_is_empty() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="analysis-missing",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in order-service logs"},
            "s1",
        )
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "add_steps"
    assert "query_redis_status" in analysis.missing_evidence
    assert "query_metrics" in analysis.missing_evidence
    assert {step.tool_name for step in analysis.recommended_steps} >= {
        "query_metrics",
        "query_redis_status",
    }


def test_analyzer_retries_failed_tool_once() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="analysis-failed-tool",
    )
    state["tool_call_records"] = [
        {
            "trace_id": state["trace_id"],
            "incident_id": state["incident"]["incident_id"],
            "step_id": "s3",
            "tool_name": "query_redis_status",
            "input_args": {"service_name": "order-service"},
            "status": "failed",
            "error_message": "redis backend unavailable",
            "latency_ms": 12,
        }
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "retry_failed_tool"
    assert analysis.retry_steps[0].tool_name == "query_redis_status"
    assert analysis.retry_steps[0].step_id == "s3-retry"
    assert analysis.retry_steps[0].retry_count == 1


def test_analyzer_does_not_retry_same_tool_after_retry_already_failed() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="analysis-retry-cap",
    )
    state["tool_call_records"] = [
        {"step_id": "s3", "tool_name": "query_redis_status", "status": "failed"},
        {"step_id": "s3-retry", "tool_name": "query_redis_status", "status": "failed"},
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision != "retry_failed_tool"
    assert analysis.retry_steps == []


def test_analyzer_detects_redis_log_status_conflict_and_lowers_confidence() -> None:
    state = create_initial_aiops_state(
        "order-service Redis timeout in logs but Redis status normal",
        session_id="analysis-conflict",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=3250ms, 5xx=8.20%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in order-service logs"},
            "s2",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "Redis connected_clients 正常",
                "connected_clients": 1200,
                "maxclients": 10000,
                "client_usage_ratio": 0.12,
                "alert_info": {"triggered": False},
            },
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "generate_report"
    assert analysis.evidence_sufficient is False
    assert analysis.conflicts
    assert "Redis" in analysis.conflicts[0]
    assert analysis.confidence < 0.72
    assert any("证据冲突降低置信度" in reason for reason in analysis.confidence_reasons)


def test_analyzer_does_not_treat_all_refuting_successes_as_sufficient() -> None:
    state = create_initial_aiops_state(
        "order-service Redis maxclients alert",
        session_id="analysis-all-refuting",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95 and 5xx are within threshold",
                "p95_latency_ms": {"status": "normal"},
                "error_rate": {"status": "normal"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {
                "summary": "no error or timeout log found",
                "signals": {"log_count": 0},
                "logs": {"total": 0, "logs": []},
            },
            "s2",
        ),
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "Redis connected_clients is normal",
                "connected_clients": 1200,
                "maxclients": 10000,
                "client_usage_ratio": 0.12,
                "alert_info": {"triggered": False},
            },
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)
    redis_hypothesis = next(
        item for item in analysis.hypothesis_ranking if item.category == "redis_maxclients"
    )

    assert analysis.evidence_sufficient is False
    assert analysis.confidence < 0.86
    assert redis_hypothesis.supporting_evidence_ids == []
    assert redis_hypothesis.refuting_evidence_ids


def test_analyzer_degrades_to_incomplete_report_after_retry_failed() -> None:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout and 5xx",
        session_id="analysis-degraded-report",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=3250ms, 5xx=8.20%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "Redis connection timeout in order-service logs"},
            "s2",
        ),
    ]
    state["tool_call_records"] = [
        {"step_id": "s3", "tool_name": "query_redis_status", "status": "failed"},
        {
            "step_id": "s3-retry",
            "tool_name": "query_redis_status",
            "status": "failed",
            "error_message": "redis backend unavailable",
        },
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "generate_report"
    assert analysis.evidence_sufficient is False
    assert analysis.retry_steps == []
    assert "降级生成不完整诊断" in analysis.reason
    assert any("工具失败降级" in reason for reason in analysis.confidence_reasons)


def test_failed_tool_payload_does_not_create_root_cause_hypothesis() -> None:
    state = create_initial_aiops_state(
        "order-service request failures",
        session_id="analysis-failed-payload-hypothesis",
    )
    state["incident"]["service_name"] = "order-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_redis_status",
            {
                "summary": "connected_clients reached maxclients",
                "connected_clients": 10000,
                "maxclients": 10000,
                "error_message": "redis backend unavailable",
            },
            "s3-retry",
            status="failed",
        )
    ]
    state["tool_call_records"] = [
        {
            "step_id": "s3-retry",
            "tool_name": "query_redis_status",
            "status": "failed",
            "error_message": "redis backend unavailable",
        }
    ]

    analysis = analyze_evidence(state)

    assert not any("Redis" in item and "maxclients" in item for item in analysis.hypotheses)
    assert not any(item.category == "redis_maxclients" for item in analysis.hypothesis_ranking)
    assert analysis.decision != "generate_report"


def test_stale_tool_payload_does_not_create_root_cause_hypothesis() -> None:
    state = create_initial_aiops_state(
        "order-service request failures",
        session_id="analysis-stale-payload-hypothesis",
    )
    evidence = evidence_from_tool(
        "query_redis_status",
        {
            "summary": "connected_clients reached maxclients",
            "connected_clients": 10000,
            "maxclients": 10000,
            "stale": True,
        },
        "s-stale",
    )
    evidence["stance"] = "unknown"
    evidence["raw_data"]["metadata"] = {
        "evidence_quality": {
            "status": "stale",
            "usable": False,
            "reasons": ["result_marked_stale_or_expired"],
        }
    }
    state["gathered_evidence"] = [evidence]

    analysis = analyze_evidence(state)

    assert not any(item.category == "redis_maxclients" for item in analysis.hypothesis_ranking)


def test_stale_success_is_still_reported_as_missing_tool_evidence() -> None:
    state = create_initial_aiops_state(
        "order-service Redis maxclients alert",
        session_id="analysis-stale-success-missing",
    )
    evidence = evidence_from_tool(
        "query_redis_status",
        {
            "summary": "connected_clients reached maxclients",
            "connected_clients": 10000,
            "maxclients": 10000,
        },
        "s-stale-missing",
    )
    evidence["stance"] = "unknown"
    evidence["raw_data"]["metadata"] = {
        "evidence_quality": {
            "status": "stale",
            "usable": False,
            "reasons": ["result_marked_stale_or_expired"],
        }
    }
    state["gathered_evidence"] = [evidence]

    analysis = analyze_evidence(state)

    assert "query_redis_status" in analysis.missing_evidence


def test_failed_and_unclassified_evidence_use_unknown_stance() -> None:
    failed_stance = infer_evidence_stance(
        source_tool="query_redis_status",
        raw_data={"status": "failed", "error_message": "redis backend unavailable"},
        summary="Redis 查询失败",
    )
    unclassified_stance = infer_evidence_stance(
        source_tool="query_unregistered_system",
        raw_data={"status": "success", "output": {"summary": "legacy observation"}},
        summary="legacy observation",
    )

    assert failed_stance == "unknown"
    assert build_confidence_reason(
        source_tool="query_redis_status",
        raw_data={"status": "failed", "error_message": "redis backend unavailable"},
        stance=failed_stance,
    ).startswith("工具失败")
    assert unclassified_stance == "unknown"


def test_reference_results_are_neutral_context_not_direct_support() -> None:
    for source_tool in ["search_runbook", "search_history_ticket"]:
        stance = infer_evidence_stance(
            source_tool=source_tool,
            raw_data={
                "status": "success",
                "output": {"summary": "similar Redis maxclients guidance"},
            },
            summary="similar Redis maxclients guidance",
        )

        assert stance == "neutral"


def test_analyzer_ranks_mysql_slow_query_hypothesis() -> None:
    state = create_initial_aiops_state(
        "payment-service MySQL slow query and SQL timeout",
        session_id="analysis-mysql-ranking",
    )
    state["incident"]["service_name"] = "payment-service"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_mysql_status",
            {
                "summary": "MySQL 慢查询累计增加，连接池等待",
                "slow_queries": [{"sql_digest": "select * from payment", "avg_ms": 800}],
                "connections": {"active": 188, "max": 200},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "SQL timeout in payment-service"},
            "s2",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.hypothesis_ranking[0].category == "mysql_slow_query"
    assert "MySQL" in analysis.hypothesis_ranking[0].title
    assert analysis.hypothesis_ranking[0].supporting_evidence_ids


def test_evidence_type_alone_does_not_match_unrelated_category() -> None:
    assert (
        evidence_matches_category(
            "redis_maxclients",
            "redis",
            "redis status is healthy and no saturation signal is present",
            ["redis", "maxclients"],
        )
        is False
    )
    assert (
        evidence_matches_category(
            "k8s_crashloop",
            "k8s",
            "pod was oomkilled after exceeding its memory limit",
            ["crashloop", "pod"],
        )
        is False
    )


def test_flat_error_rate_uses_ratio_units_for_abnormal_metric_detection() -> None:
    assert is_metric_abnormal({"error_rate": 0.082}) is True
    assert is_metric_abnormal({"error_rate": 0.001}) is False


def test_nested_metric_current_values_are_checked_without_status_labels() -> None:
    assert is_metric_abnormal({"p95_latency_ms": {"current": 3200}}) is True
    assert is_metric_abnormal({"error_rate": {"current": 0.082}}) is True
    assert (
        is_metric_abnormal(
            {
                "p95_latency_ms": {"current": 120},
                "error_rate": {"current": 0.001},
            }
        )
        is False
    )


def test_default_unknown_identity_does_not_create_human_handoff_hypothesis() -> None:
    state = create_initial_aiops_state(
        "checkout-service latency investigation",
        session_id="analysis-unknown-defaults",
    )
    state["incident"].update(
        {
            "service_name": "unknown-service",
            "environment": "unknown",
            "symptom": "P95 latency increased",
        }
    )
    state["gathered_evidence"] = []

    analysis = analyze_evidence(state)

    assert not any(item.category == "unknown_needs_human" for item in analysis.hypothesis_ranking)


def test_incomplete_metric_output_is_unknown_not_refuting() -> None:
    stance = infer_evidence_stance(
        source_tool="query_metrics",
        raw_data={"status": "success", "output": {"qps": 120}},
        summary="QPS observed",
    )

    assert stance == "unknown"


def test_incomplete_redis_output_is_unknown_not_refuting() -> None:
    stance = infer_evidence_stance(
        source_tool="query_redis_status",
        raw_data={"status": "success", "output": {"redis_version": "7.2"}},
        summary="Redis status returned",
    )

    assert stance == "unknown"


def test_mysql_zero_value_signal_is_not_supporting() -> None:
    stance = infer_evidence_stance(
        source_tool="query_mysql_status",
        raw_data={
            "status": "success",
            "output": {
                "slow_queries": [],
                "signals": {"slow_query_count": 0, "pool_waiting": 0},
            },
        },
        summary="slow_query_count=0, pool_waiting=0",
    )

    assert stance == "refuting"


def test_missing_tools_use_category_specific_matching() -> None:
    missing = missing_tools_from_context(
        {"query_metrics", "query_logs"},
        "pod restarted once after a routine deployment",
    )

    assert "query_k8s_status" not in missing


def test_generic_timeout_does_not_match_dependency_timeout_category() -> None:
    assert (
        evidence_matches_category(
            "dependency_timeout",
            "metric",
            "order-service Redis connection timeout",
            ["dependency", "timeout"],
        )
        is False
    )


def test_generic_restart_does_not_match_crashloop_category() -> None:
    assert (
        evidence_matches_category(
            "k8s_crashloop",
            "k8s",
            "pod restarted once after deployment",
            ["crashloop", "restart"],
        )
        is False
    )


def test_normal_metric_summary_is_refuting_not_supporting() -> None:
    stance = infer_evidence_stance(
        source_tool="query_metrics",
        raw_data={
            "status": "success",
            "output": {
                "summary": "P95 is normal and 5xx is below threshold",
                "p95_latency_ms": {"current": 120, "status": "normal"},
                "error_rate": {"current": 0.001, "status": "normal"},
            },
        },
        summary="P95 is normal and 5xx is below threshold",
    )

    assert stance == "refuting"


def test_partial_metric_result_is_neutral_not_refuting() -> None:
    stance = infer_evidence_stance(
        source_tool="query_metrics",
        raw_data={
            "status": "success",
            "output": {
                "source": "prometheus",
                "summary": "P95 and error rate queries returned no series",
                "data_quality": "partial",
                "empty_queries": ["p95_latency_ms", "error_rate"],
                "p95_latency_ms": {"current": 0, "status": "missing"},
                "error_rate": {"current": 0, "status": "missing"},
            },
        },
        summary="P95 and error rate queries returned no series",
    )

    assert stance == "neutral"


def test_duplicate_observation_counts_once_for_hypothesis_confidence() -> None:
    state = create_initial_aiops_state(
        "order-service Redis maxclients alert",
        session_id="analysis-duplicate-observation",
    )
    first = evidence_from_tool(
        "query_redis_status",
        {
            "summary": "connected_clients reached maxclients",
            "connected_clients": 10000,
            "maxclients": 10000,
        },
        "s1",
    )
    second = dict(first)
    second["evidence_id"] = "evd-duplicate-copy"
    second["step_id"] = "s1-replayed"
    state["gathered_evidence"] = [first, second]

    analysis = analyze_evidence(state)
    redis_hypothesis = next(
        item for item in analysis.hypothesis_ranking if item.category == "redis_maxclients"
    )

    assert len(redis_hypothesis.supporting_evidence_ids) == 1


def test_empty_log_result_ignores_error_keywords_from_query_metadata() -> None:
    stance = infer_evidence_stance(
        source_tool="query_logs",
        raw_data={
            "status": "success",
            "output": {
                "summary": "Loki 返回 0 条 order-service 日志",
                "query": "ERROR OR timeout",
                "signals": {"log_count": 0},
                "logs": {"total": 0, "logs": []},
            },
        },
        summary="Loki 返回 0 条 order-service 日志",
    )

    assert stance == "refuting"


def test_incident_timeout_text_without_log_evidence_does_not_claim_log_observation() -> None:
    state = create_initial_aiops_state(
        "order-service Redis timeout and maxclients alert",
        session_id="analysis-alert-only-timeout",
    )
    state["incident"]["symptom"] = "Redis timeout and maxclients alert"
    state["gathered_evidence"] = []

    analysis = analyze_evidence(state)

    assert not any("错误日志出现 timeout" in item for item in analysis.hypotheses)


def test_incident_metric_text_without_metric_evidence_does_not_claim_metric_observation() -> None:
    state = create_initial_aiops_state(
        "order-service P95 and 5xx alert",
        session_id="analysis-alert-only-metrics",
    )
    state["incident"]["symptom"] = "P95 latency and 5xx error rate increased"
    state["gathered_evidence"] = []

    analysis = analyze_evidence(state)

    assert not any("服务 P95 延迟或 5xx 错误率异常升高" in item for item in analysis.hypotheses)


def test_normal_memory_text_does_not_create_false_k8s_oom_conflict() -> None:
    state = create_initial_aiops_state(
        "payment-service memory usage normal; mysql slow query",
        session_id="analysis-normal-memory",
    )
    state["incident"]["symptom"] = "memory usage normal; mysql slow query"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_mysql_status",
            {
                "summary": "MySQL slow query detected",
                "slow_queries": [{"sql_digest": "digest-1", "avg_ms": 1200}],
            },
        )
    ]

    analysis = analyze_evidence(state)

    assert not any("K8s OOM" in item for item in analysis.conflicts)


def test_mysql_golden_incident_requires_release_and_reference_evidence() -> None:
    state = create_initial_aiops_state(
        "payment-service MySQL slow query latency",
        session_id="analysis-mysql-golden",
    )
    state["incident"].update(
        {
            "service_name": "payment-service",
            "raw_alert": {
                "alertname": "MySQLSlowQueryLatency",
                "dependency": "payment-mysql",
            },
        }
    )
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_mysql_status",
            {
                "summary": "slow query digest=9f3a-pay-report, pool_waiting=6",
                "slow_queries": [{"sql_digest": "9f3a-pay-report", "avg_ms": 2280}],
                "signals": {"slow_query_count": 18, "pool_waiting": 6},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=2280ms, 5xx=1.2%, CPU=78.5%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s2",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "slow query digest=9f3a-pay-report pool_waiting=6"},
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "add_steps"
    assert {
        "query_deploy_history",
        "search_runbook",
        "search_history_ticket",
    } <= set(analysis.missing_evidence)
