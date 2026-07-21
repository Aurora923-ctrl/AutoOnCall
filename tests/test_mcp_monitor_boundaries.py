"""Boundary tests for local monitor MCP helpers."""

import pytest

pytest.importorskip("fastmcp")

from mcp_servers.cls_server import search_log, search_topic_by_service_name
from mcp_servers.monitor_server import (
    invalid_metric_interval_payload,
    parse_interval_minutes,
    query_cpu_metrics,
    query_memory_metrics,
)


def test_monitor_interval_parser_rejects_zero_interval() -> None:
    assert parse_interval_minutes("5m") == 5
    assert parse_interval_minutes("1h") == 60

    with pytest.raises(ValueError, match="greater than 0"):
        parse_interval_minutes("0m")


def test_monitor_invalid_interval_payload_does_not_echo_exception_text() -> None:
    payload = invalid_metric_interval_payload(
        service_name="order-service",
        metric_name="cpu_usage_percent",
        interval="secret-interval",
        error=ValueError("token=super-secret"),
    )

    assert "super-secret" not in str(payload)


def test_monitor_metrics_are_explicitly_marked_as_synthetic_mock() -> None:
    cpu = query_cpu_metrics.fn(
        service_name="order-service",
        start_time="2026-07-16 10:00:00",
        end_time="2026-07-16 10:02:00",
        interval="1m",
    )
    memory = query_memory_metrics.fn(
        service_name="order-service",
        start_time="2026-07-16 10:00:00",
        end_time="2026-07-16 10:02:00",
        interval="1m",
    )

    assert cpu["status"] == "success"
    assert cpu["source"] == "mock"
    assert cpu["synthetic"] is True
    assert cpu["source_quality"] == "fallback_only"
    assert cpu["evidence_origin"] == "mcp_mock:query_cpu_metrics"
    assert memory["status"] == "success"
    assert memory["source"] == "mock"
    assert memory["synthetic"] is True
    assert memory["source_quality"] == "fallback_only"
    assert memory["evidence_origin"] == "mcp_mock:query_memory_metrics"


def test_cls_tools_use_explicit_mock_success_and_failure_envelopes() -> None:
    topic = search_topic_by_service_name.fn(service_name="data-sync-service")
    logs = search_log.fn(
        topic_id="topic-001",
        start_time=1_000,
        end_time=61_000,
        query="ERROR",
        limit=10,
    )
    missing = search_log.fn(
        topic_id="missing-topic",
        start_time=1_000,
        end_time=61_000,
        query="ERROR",
        limit=10,
    )

    assert topic["status"] == "success"
    assert topic["source"] == "mock"
    assert topic["synthetic"] is True
    assert topic["source_quality"] == "fallback_only"
    assert topic["evidence_origin"] == "mcp_mock:search_topic_by_service_name"
    assert logs["status"] == "success"
    assert logs["source"] == "mock"
    assert logs["synthetic"] is True
    assert logs["source_quality"] == "fallback_only"
    assert logs["evidence_origin"] == "mcp_mock:search_log"
    assert missing["status"] == "failed"
    assert missing["source"] == "mock"
    assert missing["synthetic"] is True
    assert missing["error_message"]
