"""Tests for structured evidence analysis."""

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import analyze_evidence
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
)
from app.tools.base import ToolExecutionResult

DEFAULT_DATA_SOURCE_BY_TOOL = {
    "query_metrics": "prometheus",
    "query_logs": "loki",
    "query_redis_status": "redis_info",
    "query_mysql_status": "mysql",
    "query_k8s_status": "kubernetes",
    "query_message_queue_status": "redpanda",
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


def test_analyzer_marks_redpanda_lag_evidence_as_report_ready() -> None:
    state = create_initial_aiops_state(
        "checkout-service 响应慢，订单消息积压，怀疑 Redpanda consumer lag",
        session_id="analysis-redpanda",
    )
    state["incident"]["service_name"] = "checkout-service"
    state["incident"]["symptom"] = "Redpanda/Kafka topic 积压，consumer lag 快速升高"
    state["gathered_evidence"] = [
        evidence_from_tool(
            "query_metrics",
            {
                "summary": "P95=2300ms, 5xx=2.50%",
                "p95_latency_ms": {"status": "high"},
                "error_rate": {"status": "high"},
            },
            "s1",
        ),
        evidence_from_tool(
            "query_logs",
            {"summary": "checkout-service publish timeout and downstream slow processing"},
            "s2",
        ),
        evidence_from_tool(
            "query_message_queue_status",
            {
                "summary": "mock Redpanda 返回 redpanda-checkout consumer lag 高",
                "source": "mock",
                "signals": {
                    "ready": True,
                    "consumer_lag": 128400,
                    "max_partition_lag": 79000,
                    "under_replicated_partitions": 0,
                },
            },
            "s3",
        ),
    ]

    analysis = analyze_evidence(state)

    assert analysis.decision == "generate_report"
    assert analysis.evidence_sufficient is False
    assert analysis.confidence <= 0.72
    assert analysis.evidence_profile["source_quality"] == "mixed_with_fallback"
    assert analysis.evidence_profile["fallback_source_count"] == 1
    assert any(item.category == "message_queue_lag" for item in analysis.hypothesis_ranking)
    assert "query_message_queue_status" not in analysis.missing_evidence
    assert any("Mock 回退证据" in reason for reason in analysis.confidence_reasons)
    assert any("置信度封顶" in reason for reason in analysis.confidence_reasons)


def test_message_queue_normal_state_refutes_lag_hypothesis() -> None:
    stance = infer_evidence_stance(
        source_tool="query_message_queue_status",
        raw_data={
            "status": "success",
            "output": {
                "source": "mock",
                "summary": "mock Redpanda 返回 topic 正常，无 consumer lag 积压",
                "signals": {
                    "ready": True,
                    "consumer_lag": 0,
                    "max_partition_lag": 0,
                    "under_replicated_partitions": 0,
                },
            },
        },
        summary="mock Redpanda 返回 topic 正常，无 consumer lag 积压",
    )

    assert stance == "refuting"


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
