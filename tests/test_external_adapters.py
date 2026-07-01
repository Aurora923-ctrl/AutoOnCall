"""Tests for production external adapters wired into AIOps tools."""

import json

import httpx
import pytest

from app.agent.aiops.executor import _postpone_risky_step_until_read_only_evidence_complete
from app.agent.aiops.risk_controller import RiskControlDecision
from app.config import Settings, config
from app.integrations.alertmanager import AlertmanagerAlertAdapter
from app.integrations.base import ExternalAdapterError, adapter_failure
from app.integrations.kubernetes import KubernetesStatusAdapter
from app.integrations.log_gateway import HTTPLogGatewayAdapter
from app.integrations.loki import LokiLogAdapter
from app.integrations.mysql import MySQLStatusAdapter
from app.integrations.prometheus import PrometheusMetricsAdapter
from app.integrations.redis_info import RedisInfoAdapter
from app.integrations.redpanda import RedpandaStatusAdapter
from app.integrations.service_catalog import DeployHistoryAdapter
from app.integrations.ticketing import TicketingAdapter
from app.integrations.tracing import TracingAdapter
from app.models.evidence import normalize_data_source
from app.models.plan import PlanStep
from app.tools.alert_tool import QueryAlertsTool
from app.tools.context_tool import QueryDeployHistoryTool, QueryServiceContextTool
from app.tools.logs_tool import QueryLogsTool
from app.tools.message_queue_tool import QueryMessageQueueStatusTool
from app.tools.metrics_tool import QueryMetricsTool
from app.tools.mock_ops_tool import (
    QueryK8sStatusTool,
    QueryMySQLStatusTool,
    SearchHistoryTicketTool,
    SuggestRemediationTool,
)
from app.tools.redis_tool import QueryRedisStatusTool
from app.tools.tracing_tool import QueryTracesTool


class FakeMetricsAdapter:
    configured = True

    async def query_service_metrics(self, service_name: str, time_range: str, interval: str):
        return {
            "status": "success",
            "service_name": service_name,
            "time_range": time_range,
            "interval": interval,
            "source": "prometheus",
            "signals": {"p95_latency_ms": 321, "error_rate": 0.001},
            "raw": {"fixture": "prometheus"},
            "qps": {"current": 42},
            "p95_latency_ms": {"current": 321, "status": "normal"},
            "error_rate": {"current": 0.001, "status": "normal"},
            "cpu": {"metric_name": "cpu_usage_percent"},
            "memory": {"metric_name": "memory_working_set_bytes"},
            "summary": "prometheus ok",
        }


class FakeAlertAdapter:
    configured = True

    async def query_alerts(self, service_name: str, state: str = "active", limit: int = 20):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "alertmanager",
            "signals": {"alert_count": 1, "firing_count": 1},
            "raw": {"alerts": [{"labels": {"service": service_name}}]},
            "alerts": [{"alertname": "HighErrorRate", "service_name": service_name}],
            "summary": "alertmanager ok",
        }


class FailingMetricsAdapter:
    configured = True

    async def query_service_metrics(self, service_name: str, time_range: str, interval: str):
        raise RuntimeError("prometheus unavailable")


class FakeLogGateway:
    configured = True

    async def search_logs(self, service_name: str, query: str, time_range: str, limit: int):
        return {
            "status": "success",
            "service_name": service_name,
            "query": query,
            "source": "log_gateway",
            "signals": {"log_count": 1},
            "raw": {"logs": [{"message": "real error"}]},
            "logs": {"total": 1, "logs": [{"message": "real error"}]},
            "summary": "gateway ok",
        }


class FakeLokiAdapter:
    configured = True

    async def search_logs(self, service_name: str, query: str, time_range: str, limit: int):
        return {
            "status": "success",
            "service_name": service_name,
            "query": query,
            "source": "loki",
            "signals": {"log_count": 1},
            "raw": {"data": {"result": []}},
            "logs": {"total": 1, "logs": [{"message": "RedisConnectionTimeout"}]},
            "summary": "loki ok",
        }


class UnconfiguredLokiAdapter:
    configured = False


class FakeTracingAdapter:
    configured = True

    async def query_service_traces(self, service_name: str, lookback: str = "1h", limit: int = 20):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "jaeger",
            "signals": {"trace_count": 1, "error_span_count": 0},
            "raw": {"trace_count": 1},
            "traces": [{"trace_id": "abc", "span_count": 3}],
            "summary": "jaeger ok",
        }


class FakeRedpandaAdapter:
    configured = True

    async def query_status(self, service_name: str, topic: str = ""):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "redpanda",
            "signals": {"ready": True, "topic_count": 1, "partition_count": 3},
            "raw": {"ready": {"body": "ready"}},
            "topics": ["redpanda-orders"],
            "summary": "redpanda ok",
        }


class FakeRedisAdapter:
    configured = True

    async def query_status(self, service_name: str, redis_instance: str, time_range: str):
        return {
            "status": "success",
            "service_name": service_name,
            "redis_instance": redis_instance,
            "time_range": time_range,
            "source": "redis_info",
            "signals": {"connected_clients": 12, "maxclients": 1000},
            "raw": {"info": {"connected_clients": "12"}},
            "connected_clients": 12,
            "maxclients": 1000,
            "summary": "redis ok",
        }


class FakeK8sAdapter:
    configured = True

    async def query_service_status(self, service_name: str, time_range: str = "10m"):
        return {
            "status": "success",
            "service_name": service_name,
            "time_range": time_range,
            "source": "kubernetes",
            "signals": {"pod_count": 1},
            "raw": {"items": []},
            "pods": [{"name": "order-service-1", "ready": True}],
            "events": [],
            "summary": "k8s ok",
        }


class FakeMySQLAdapter:
    configured = True

    async def query_status(self, service_name: str, mysql_instance: str = ""):
        return {
            "status": "success",
            "service_name": service_name,
            "mysql_instance": mysql_instance,
            "source": "mysql",
            "signals": {"active_connections": 3},
            "raw": {"status": []},
            "connections": {"active": 3},
            "slow_queries": [],
            "summary": "mysql ok",
        }


class FakeTicketingAdapter:
    configured = True

    async def search_history(self, service_name: str, query: str = "", limit: int = 5):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "ticket_api",
            "signals": {"ticket_count": 1},
            "raw": {"items": [{"ticket_id": "INC-1"}]},
            "tickets": [{"ticket_id": "INC-1"}],
            "summary": "ticket ok",
        }


class FakeCMDBAdapter:
    configured = True

    async def query_service(self, service_name: str):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "cmdb",
            "signals": {"dependency_count": 2},
            "raw": {"service": {"service_name": service_name}},
            "service": {"service_name": service_name, "owner": "payments-oncall"},
            "dependencies": ["redis-cluster-prod", "order-mysql"],
            "summary": "cmdb ok",
        }


class FakeDeployHistoryAdapter:
    configured = True

    async def query_deployments(self, service_name: str, limit: int = 5):
        return {
            "status": "success",
            "service_name": service_name,
            "source": "deploy_history",
            "signals": {"deployment_count": 1, "latest_status": "succeeded"},
            "raw": {"deployments": [{"version": "2026.06.27"}]},
            "deployments": [{"version": "2026.06.27", "status": "succeeded"}],
            "recent_change": {"version": "2026.06.27", "status": "succeeded"},
            "summary": "deploy history ok",
        }


def test_adapter_failure_classifies_timeout_and_http_status() -> None:
    timeout_payload = adapter_failure("prometheus", TimeoutError("query timeout"))
    assert timeout_payload["status"] == "failed"
    assert timeout_payload["error_type"] == "timeout"
    assert timeout_payload["retryable"] is True
    assert timeout_payload["signals"] == {}
    assert timeout_payload["raw"] == {}

    request = httpx.Request("GET", "https://kubernetes.example/api")
    response = httpx.Response(403, request=request)
    permission_payload = adapter_failure(
        "kubernetes",
        httpx.HTTPStatusError("forbidden", request=request, response=response),
    )
    assert permission_payload["error_type"] == "permission_denied"
    assert permission_payload["retryable"] is False


def test_settings_derives_redis_and_mysql_urls_from_compatible_fields() -> None:
    redis_settings = Settings(
        _env_file=None,
        redis_url="redis://cache.local:6380/2",
        redis_host="",
    )
    assert redis_settings.resolved_redis_url == "redis://cache.local:6380/2"

    mysql_url_settings = Settings(
        _env_file=None,
        mysql_dsn="",
        mysql_url="mysql+pymysql://u:p@db.local:3306/app",
    )
    assert mysql_url_settings.resolved_mysql_dsn == "mysql+pymysql://u:p@db.local:3306/app"

    split_mysql_settings = Settings(
        _env_file=None,
        mysql_dsn="",
        mysql_url="",
        mysql_host="127.0.0.1",
        mysql_port=3307,
        mysql_user="diag",
        mysql_password="p@ss word",
        mysql_database="autooncall",
    )
    assert split_mysql_settings.resolved_mysql_dsn == (
        "mysql+pymysql://diag:p%40ss%20word@127.0.0.1:3307/autooncall"
    )


def test_settings_parses_instance_maps() -> None:
    settings = Settings(
        _env_file=None,
        redis_instances='{"redis-cluster-prod":"redis://127.0.0.1:6379/0"}',
        mysql_instances='{"order-mysql":"mysql+pymysql://u:p@127.0.0.1:3306/app"}',
    )

    assert settings.redis_instance_map["redis-cluster-prod"] == "redis://127.0.0.1:6379/0"
    assert settings.mysql_instance_map["order-mysql"].startswith("mysql+pymysql://")


def test_settings_uses_dashscope_model_when_rag_model_is_empty() -> None:
    inherited = Settings(_env_file=None, dashscope_model="qwen-plus", rag_model="")
    overridden = Settings(_env_file=None, dashscope_model="qwen-plus", rag_model="qwen-max")

    assert inherited.effective_rag_model == "qwen-plus"
    assert overridden.effective_rag_model == "qwen-max"


def test_settings_exposes_loguru_retention_text() -> None:
    configured = Settings(_env_file=None, log_retention_days=21)
    bounded = Settings(_env_file=None, log_retention_days=0)

    assert configured.log_file_retention == "21 days"
    assert bounded.log_file_retention == "1 days"


def test_settings_exposes_upload_runtime_config() -> None:
    settings = Settings(
        _env_file=None,
        upload_allowed_extensions="txt,.md, markdown",
        upload_max_file_size_mb=3,
    )
    bounded = Settings(_env_file=None, upload_max_file_size_mb=0)

    assert settings.upload_allowed_extension_list == ["txt", "md", "markdown"]
    assert settings.upload_max_file_size == 3 * 1024 * 1024
    assert bounded.upload_max_file_size == 1024 * 1024


@pytest.mark.asyncio
async def test_alertmanager_adapter_filters_and_normalizes_alerts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/alerts"
        return httpx.Response(
            200,
            json=[
                {
                    "labels": {
                        "alertname": "HighErrorRate",
                        "service": "order-service",
                        "severity": "critical",
                    },
                    "annotations": {"summary": "order-service 5xx high"},
                    "status": {"state": "active"},
                    "startsAt": "2026-06-28T00:00:00Z",
                },
                {
                    "labels": {"alertname": "OtherAlert", "service": "payment-service"},
                    "status": {"state": "active"},
                },
            ],
        )

    adapter = AlertmanagerAlertAdapter(
        base_url="https://alertmanager.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_alerts("order-service")

    assert result["source"] == "alertmanager"
    assert result["signals"]["alert_count"] == 1
    assert result["alerts"][0]["alertname"] == "HighErrorRate"


@pytest.mark.asyncio
async def test_prometheus_adapter_marks_empty_query_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": []}},
        )

    adapter = PrometheusMetricsAdapter(
        base_url="https://prometheus.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_service_metrics("order-service")

    assert result["status"] == "success"
    assert result["source"] == "prometheus"
    assert result["signals"]["p95_latency_ms"] == 0
    assert set(result["empty_queries"]) == {
        "qps",
        "error_rate",
        "p95_latency_ms",
        "cpu_usage_percent",
        "memory_working_set_bytes",
    }
    assert result["raw"]["empty_queries"] == result["empty_queries"]


@pytest.mark.asyncio
async def test_log_gateway_adapter_sends_window_filters_and_bounded_limit() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"logs": [{"message": "timeout"}]})

    adapter = HTTPLogGatewayAdapter(
        url="https://logs.example/search",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.search_logs(
        "order-service",
        query="ERROR OR timeout",
        time_range="15m",
        limit=5000,
    )

    assert captured["service_name"] == "order-service"
    assert captured["time_range"] == "15m"
    assert captured["keyword_filters"] == ["ERROR", "timeout"]
    assert captured["limit"] == 1000
    assert captured["end_time"] > captured["start_time"]
    assert result["signals"]["log_count"] == 1
    assert result["keyword_filters"] == ["ERROR", "timeout"]


@pytest.mark.asyncio
async def test_loki_adapter_queries_range_and_normalizes_streams() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.params.get("query"))
        captured["limit"] = str(request.url.params.get("limit"))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "order-service", "level": "error"},
                            "values": [["1780000000000000000", "RedisConnectionTimeout"]],
                        }
                    ]
                },
            },
        )

    adapter = LokiLogAdapter(
        base_url="https://loki.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.search_logs("order-service", "ERROR OR timeout", "10m", 5000)

    assert captured["path"] == "/loki/api/v1/query_range"
    assert '{service="order-service"}' in captured["query"]
    assert captured["limit"] == "1000"
    assert result["source"] == "loki"
    assert result["signals"]["log_count"] == 1
    assert result["logs"]["logs"][0]["message"] == "RedisConnectionTimeout"


@pytest.mark.asyncio
async def test_tracing_adapter_queries_jaeger_and_summarizes_spans() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/traces"
        assert request.url.params.get("service") == "order-service"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "traceID": "trace-1",
                        "duration": 2500000,
                        "startTime": 1780000000000000,
                        "processes": {"p1": {"serviceName": "order-service"}},
                        "spans": [
                            {
                                "processID": "p1",
                                "tags": [{"key": "error", "value": True}],
                            },
                            {"processID": "p1", "tags": []},
                        ],
                    }
                ]
            },
        )

    adapter = TracingAdapter(
        jaeger_url="https://jaeger.example",
        tempo_url="https://tempo.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_service_traces("order-service")

    assert result["source"] == "jaeger"
    assert result["signals"]["trace_count"] == 1
    assert result["signals"]["error_span_count"] == 1
    assert result["traces"][0]["services"] == ["order-service"]
    assert result["tempo_configured"] is True


@pytest.mark.asyncio
async def test_tracing_adapter_queries_tempo_when_jaeger_is_unconfigured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/search"
        assert 'resource.service.name = "order-service"' in request.url.params.get("q", "")
        return httpx.Response(
            200,
            json={
                "traces": [
                    {
                        "traceID": "trace-tempo-1",
                        "durationMs": 2500,
                        "startTimeUnixNano": "1780000000000000000",
                        "rootServiceName": "order-service",
                        "spanSets": [
                            {
                                "spans": [
                                    {
                                        "attributes": {
                                            "resource.service.name": "order-service",
                                            "status.code": "ERROR",
                                        }
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    adapter = TracingAdapter(
        jaeger_url="",
        tempo_url="https://tempo.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_service_traces("order-service")

    assert result["source"] == "tempo"
    assert result["backend"] == "tempo"
    assert result["signals"]["trace_count"] == 1
    assert result["signals"]["error_span_count"] == 1
    assert result["signals"]["slowest_duration_us"] == 2500000
    assert result["traces"][0]["services"] == ["order-service"]


@pytest.mark.asyncio
async def test_redpanda_adapter_reads_admin_readiness_and_partitions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/status/ready":
            return httpx.Response(200, text="ready")
        if request.url.path == "/v1/partitions":
            return httpx.Response(
                200,
                json=[
                    {"topic": "redpanda-orders", "partition_id": 0},
                    {"topic": "redpanda-payments", "partition_id": 0},
                ],
            )
        return httpx.Response(404)

    adapter = RedpandaStatusAdapter(
        admin_url="https://redpanda.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_status("checkout-service", topic="redpanda-orders")

    assert result["source"] == "redpanda"
    assert result["signals"]["ready"] is True
    assert result["signals"]["topic_count"] == 2
    assert result["signals"]["matched_partition_count"] == 1


def test_redpanda_adapter_requires_admin_url_not_only_kafka_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(config, "redpanda_admin_url", "")
    monkeypatch.setattr(config, "kafka_bootstrap_servers", "127.0.0.1:9092")

    adapter = RedpandaStatusAdapter()

    assert adapter.configured is False


@pytest.mark.asyncio
async def test_deploy_history_adapter_accepts_sandbox_recent_deployments_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/deployments/order-service.json"
        return httpx.Response(
            200,
            json={
                "service_name": "order-service",
                "recent_deployments": [
                    {"version": "2026.06.27-1024", "status": "succeeded"},
                    {"version": "2026.06.26-1810", "status": "succeeded"},
                ],
            },
        )

    adapter = DeployHistoryAdapter(
        url="https://deploy-history.example",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.query_deployments("order-service", limit=5)

    assert result["source"] == "deploy_history"
    assert result["signals"]["deployment_count"] == 2
    assert result["recent_change"]["version"] == "2026.06.27-1024"


@pytest.mark.asyncio
async def test_kubernetes_adapter_returns_pods_events_and_restart_signals(monkeypatch) -> None:
    monkeypatch.setattr(config, "kubernetes_api_server", "https://k8s.example")
    monkeypatch.setattr(config, "kubernetes_namespace", "prod")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pods"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "order-service-1"},
                            "status": {
                                "phase": "Running",
                                "hostIP": "10.0.0.1",
                                "containerStatuses": [
                                    {"ready": True, "restartCount": 2},
                                    {"ready": True, "restartCount": 1},
                                ],
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "involvedObject": {"name": "order-service-1"},
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container",
                            "count": 3,
                        },
                        {
                            "involvedObject": {"name": "other-service-1"},
                            "type": "Warning",
                            "reason": "Ignored",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    adapter = KubernetesStatusAdapter(transport=httpx.MockTransport(handler))

    result = await adapter.query_service_status("order-service", "5m")

    assert result["source"] == "kubernetes"
    assert result["signals"]["pod_count"] == 1
    assert result["signals"]["restart_count"] == 3
    assert result["signals"]["warning_event_count"] == 1
    assert result["events"][0]["reason"] == "BackOff"
    assert result["partial_errors"] == []


def test_redis_contract_helpers_report_slowlog_and_big_key_boundaries() -> None:
    info = RedisInfoAdapter._parse_info(
        "\n".join(
            [
                "# Clients",
                "connected_clients:9",
                "blocked_clients:1",
                "used_memory:900",
                "maxmemory:1000",
                "db0:keys=42,expires=0,avg_ttl=0",
            ]
        )
    )

    assert info["connected_clients"] == "9"
    assert RedisInfoAdapter._parse_maxclients("maxclients\n10") == 10

    big_key = RedisInfoAdapter._big_key_analysis(info)
    assert big_key["status"] == "not_scanned"
    assert big_key["risk_level"] == "high"
    assert big_key["memory_usage_ratio"] == 0.9
    assert big_key["db_key_counts"] == {"db0": 42}


def test_redis_adapter_resolves_instances_and_compacts_info(monkeypatch) -> None:
    monkeypatch.setattr(config, "redis_url", "")
    monkeypatch.setattr(config, "redis_host", "")
    monkeypatch.setattr(
        config,
        "redis_instances",
        '{"redis-cluster-prod":"redis://:secret@127.0.0.1:6380/0"}',
    )

    adapter = RedisInfoAdapter()
    target = adapter._resolve_target("redis-cluster-prod")
    assert target["host"] == "127.0.0.1"
    assert target["port"] == 6380
    assert target["password"] == "secret"

    compact = RedisInfoAdapter._compact_info(
        {"connected_clients": "1", "cmdstat_get": "large", "db0": "keys=1"}
    )
    assert compact == {
        "connected_clients": "1",
        "db0": "keys=1",
        "_raw_truncated": "true",
    }


def test_redis_contract_marks_missing_required_fields() -> None:
    info = RedisInfoAdapter._parse_info("connected_clients:9")

    assert RedisInfoAdapter._missing_required_info_fields(info, 0) == [
        "blocked_clients",
        "maxclients",
    ]


def test_mysql_adapter_blocks_non_readonly_sql_and_redacts_processlist() -> None:
    MySQLStatusAdapter._assert_read_only_sql("SHOW FULL PROCESSLIST")
    MySQLStatusAdapter._assert_read_only_sql("select 1")

    with pytest.raises(ExternalAdapterError):
        MySQLStatusAdapter._assert_read_only_sql("DELETE FROM orders")

    row = MySQLStatusAdapter._redact_process_row(
        {"Id": 1, "Info": "select * from orders where user_id=123 and phone='13800138000'"}
    )
    assert row["Info"] == "select * from orders where user_id=? and phone='?'"


def test_mysql_adapter_resolves_instance_dsn_and_compacts_payload(monkeypatch) -> None:
    monkeypatch.setattr(config, "mysql_dsn", "")
    monkeypatch.setattr(config, "mysql_url", "")
    monkeypatch.setattr(config, "mysql_host", "")
    monkeypatch.setattr(
        config,
        "mysql_instances",
        '{"order-mysql":"mysql+pymysql://diag:pw@127.0.0.1:3306/autooncall"}',
    )

    adapter = MySQLStatusAdapter()
    assert adapter._resolve_dsn("order-mysql").endswith("/autooncall")
    assert adapter._compact_status({"Threads_connected": "3", "Other": "x"}) == {
        "Threads_connected": "3"
    }


def test_mysql_status_contract_detects_empty_and_changed_response() -> None:
    assert (
        MySQLStatusAdapter._normalize_status_rows(
            [
                {"Variable_name": "Threads_connected", "Value": "3"},
                {"Variable_name": "Max_used_connections", "Value": "7"},
                {"Variable_name": "Slow_queries", "Value": "2"},
                {"Variable_name": "Innodb_row_lock_waits", "Value": "0"},
            ]
        )["Slow_queries"]
        == "2"
    )

    assert MySQLStatusAdapter._missing_required_status_fields({}) == [
        "Threads_connected",
        "Max_used_connections",
        "Slow_queries",
        "Innodb_row_lock_waits",
    ]

    with pytest.raises(ExternalAdapterError):
        MySQLStatusAdapter._normalize_status_rows([{"name": "Threads_connected"}])


@pytest.mark.asyncio
async def test_ticketing_adapter_create_ticket_handles_duplicate_and_approval_fields() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(
            409,
            json={
                "ticket": {
                    "ticket_id": "INC-1",
                    "title": "Redis maxclients exhausted",
                    "status": "open",
                    "approval_id": "apr-1",
                    "risk_action": "increase maxclients",
                }
            },
        )

    adapter = TicketingAdapter(
        url="https://ticket.example/incidents",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.create_ticket(
        service_name="order-service",
        title="Redis maxclients exhausted",
        description="connected_clients is near maxclients",
        severity="P1",
        approval_id="apr-1",
        risk_action="increase maxclients",
        idempotency_key="incident-1",
    )

    assert captured["approval_id"] == "apr-1"
    assert captured["risk_action"] == "increase maxclients"
    assert result["status"] == "success"
    assert result["ticket_status"] == "duplicate"
    assert result["duplicate"] is True
    assert result["ticket"]["approval_id"] == "apr-1"


@pytest.mark.asyncio
async def test_metrics_tool_uses_prometheus_adapter_when_configured() -> None:
    result = await QueryMetricsTool(prometheus_adapter=FakeMetricsAdapter()).arun(
        {"service_name": "order-service", "time_range": "5m", "interval": "30s"}
    )

    assert result.status == "success"
    assert result.output["source"] == "prometheus"
    assert result.output["status"] == "success"
    assert result.output["signals"]["p95_latency_ms"] == 321
    assert result.output["raw"]["fixture"] == "prometheus"
    assert result.output["qps"]["current"] == 42


@pytest.mark.asyncio
async def test_alerts_tool_uses_alertmanager_adapter_when_configured() -> None:
    result = await QueryAlertsTool(alert_adapter=FakeAlertAdapter()).arun(
        {"service_name": "order-service", "state": "active"}
    )

    assert result.status == "success"
    assert result.output["source"] == "alertmanager"
    assert result.output["signals"]["alert_count"] == 1
    assert normalize_data_source(result.tool_name, result.model_dump(mode="json")) == "alertmanager"


@pytest.mark.asyncio
async def test_tracing_and_redpanda_tools_use_real_adapters_when_configured() -> None:
    trace_result = await QueryTracesTool(tracing_adapter=FakeTracingAdapter()).arun(
        {"service_name": "order-service"}
    )
    redpanda_result = await QueryMessageQueueStatusTool(
        redpanda_adapter=FakeRedpandaAdapter()
    ).arun({"service_name": "checkout-service"})

    assert trace_result.status == "success"
    assert trace_result.output["source"] == "jaeger"
    assert (
        normalize_data_source(trace_result.tool_name, trace_result.model_dump(mode="json"))
        == "jaeger"
    )
    assert redpanda_result.status == "success"
    assert redpanda_result.output["source"] == "redpanda"
    assert (
        normalize_data_source(redpanda_result.tool_name, redpanda_result.model_dump(mode="json"))
        == "redpanda"
    )


@pytest.mark.asyncio
async def test_structured_adapter_failure_marks_tool_result_failed() -> None:
    result = await QueryMetricsTool(prometheus_adapter=FailingMetricsAdapter()).arun(
        {"service_name": "order-service", "time_range": "5m", "interval": "30s"}
    )

    assert result.status == "failed"
    assert result.output["status"] == "failed"
    assert result.output["error_type"] == "adapter_error"
    assert result.output["retryable"] is False
    assert result.output["signals"] == {}
    assert result.output["raw"] == {}
    assert result.error_message == "prometheus unavailable"
    assert "降级" not in result.output["summary"]


@pytest.mark.asyncio
async def test_redis_and_mysql_failed_results_use_failed_data_source() -> None:
    class MissingFieldRedisAdapter:
        configured = True

        async def query_status(self, service_name: str, redis_instance: str, time_range: str):
            raise ExternalAdapterError("Redis INFO response missing required fields: maxclients")

    class PermissionDeniedMySQLAdapter:
        configured = True

        async def query_status(self, service_name: str):
            raise RuntimeError("MySQL permission denied for SHOW GLOBAL STATUS")

    redis_result = await QueryRedisStatusTool(redis_adapter=MissingFieldRedisAdapter()).arun(
        {"service_name": "order-service"}
    )
    mysql_result = await QueryMySQLStatusTool(mysql_adapter=PermissionDeniedMySQLAdapter()).arun(
        {"service_name": "order-service"}
    )

    assert redis_result.status == "failed"
    assert redis_result.output["source"] == "redis_info"
    assert redis_result.output["error_type"] == "adapter_error"
    assert (
        normalize_data_source(
            redis_result.tool_name,
            redis_result.model_dump(mode="json"),
        )
        == "failed"
    )
    assert mysql_result.status == "failed"
    assert mysql_result.output["source"] == "mysql"
    assert mysql_result.output["error_type"] == "permission_denied"
    assert (
        normalize_data_source(
            mysql_result.tool_name,
            mysql_result.model_dump(mode="json"),
        )
        == "failed"
    )


@pytest.mark.asyncio
async def test_structured_adapter_failure_classifies_permission_denied() -> None:
    class PermissionDeniedK8sAdapter:
        configured = True

        async def query_service_status(self, service_name: str):
            raise RuntimeError("Kubernetes API RBAC permission denied")

    result = await QueryK8sStatusTool(k8s_adapter=PermissionDeniedK8sAdapter()).arun(
        {"service_name": "order-service"}
    )

    assert result.status == "failed"
    assert result.output["error_type"] == "permission_denied"
    assert result.output["retryable"] is False
    assert result.output["pods"] == []


@pytest.mark.asyncio
async def test_logs_tool_uses_http_gateway_when_configured() -> None:
    result = await QueryLogsTool(
        log_gateway=FakeLogGateway(),
        loki_adapter=UnconfiguredLokiAdapter(),
    ).arun({"service_name": "order-service", "query": "ERROR", "limit": 10})

    assert result.status == "success"
    assert result.output["source"] == "log_gateway"
    assert result.output["signals"]["log_count"] == 1
    assert result.output["logs"]["total"] == 1


@pytest.mark.asyncio
async def test_logs_tool_prefers_loki_when_configured() -> None:
    result = await QueryLogsTool(
        log_gateway=FakeLogGateway(),
        loki_adapter=FakeLokiAdapter(),
    ).arun({"service_name": "order-service", "query": "ERROR", "limit": 10})

    assert result.status == "success"
    assert result.output["source"] == "loki"
    assert normalize_data_source(result.tool_name, result.model_dump(mode="json")) == "loki"


@pytest.mark.asyncio
async def test_platform_tools_use_real_adapters_when_configured() -> None:
    redis_result = await QueryRedisStatusTool(redis_adapter=FakeRedisAdapter()).arun(
        {"service_name": "order-service"}
    )
    k8s_result = await QueryK8sStatusTool(k8s_adapter=FakeK8sAdapter()).arun(
        {"service_name": "order-service"}
    )
    mysql_result = await QueryMySQLStatusTool(mysql_adapter=FakeMySQLAdapter()).arun(
        {"service_name": "order-service"}
    )
    ticket_result = await SearchHistoryTicketTool(ticketing_adapter=FakeTicketingAdapter()).arun(
        {"service_name": "order-service", "query": "redis timeout"}
    )
    cmdb_result = await QueryServiceContextTool(cmdb_adapter=FakeCMDBAdapter()).arun(
        {"service_name": "order-service"}
    )
    deploy_result = await QueryDeployHistoryTool(
        deploy_history_adapter=FakeDeployHistoryAdapter()
    ).arun({"service_name": "order-service"})

    assert redis_result.output["source"] == "redis_info"
    assert k8s_result.output["source"] == "kubernetes"
    assert mysql_result.output["source"] == "mysql"
    assert mysql_result.output["mysql_instance"] == "order-mysql"
    assert ticket_result.output["source"] == "ticket_api"
    assert cmdb_result.output["source"] == "cmdb"
    assert deploy_result.output["source"] == "deploy_history"
    assert redis_result.output["signals"]["connected_clients"] == 12
    assert k8s_result.output["signals"]["pod_count"] == 1
    assert mysql_result.output["signals"]["active_connections"] == 3
    assert ticket_result.output["signals"]["ticket_count"] == 1
    assert cmdb_result.output["signals"]["dependency_count"] == 2
    assert deploy_result.output["signals"]["deployment_count"] == 1


@pytest.mark.asyncio
async def test_tools_return_not_configured_when_mock_fallback_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", False)
    monkeypatch.setattr(config, "alertmanager_base_url", "")
    monkeypatch.setattr(config, "prometheus_base_url", "")
    monkeypatch.setattr(config, "log_gateway_url", "")
    monkeypatch.setattr(config, "loki_base_url", "")
    monkeypatch.setattr(config, "jaeger_base_url", "")
    monkeypatch.setattr(config, "tempo_base_url", "")
    monkeypatch.setattr(config, "redpanda_admin_url", "")
    monkeypatch.setattr(config, "kafka_bootstrap_servers", "")
    monkeypatch.setattr(config, "cmdb_api_url", "")
    monkeypatch.setattr(config, "deploy_history_api_url", "")
    monkeypatch.setattr(config, "kubernetes_api_server", "")
    monkeypatch.setattr(config, "redis_url", "")
    monkeypatch.setattr(config, "redis_host", "")
    monkeypatch.setattr(config, "redis_instances", "")
    monkeypatch.setattr(config, "mysql_dsn", "")
    monkeypatch.setattr(config, "mysql_url", "")
    monkeypatch.setattr(config, "mysql_host", "")
    monkeypatch.setattr(config, "mysql_instances", "")
    monkeypatch.setattr(config, "ticket_api_url", "")

    alerts_result = await QueryAlertsTool().arun({"service_name": "order-service"})
    metrics_result = await QueryMetricsTool().arun({"service_name": "order-service"})
    logs_result = await QueryLogsTool().arun({"service_name": "order-service"})
    traces_result = await QueryTracesTool().arun({"service_name": "order-service"})
    redpanda_result = await QueryMessageQueueStatusTool().arun({"service_name": "checkout-service"})
    redis_result = await QueryRedisStatusTool().arun({"service_name": "order-service"})
    k8s_result = await QueryK8sStatusTool().arun({"service_name": "order-service"})
    mysql_result = await QueryMySQLStatusTool().arun({"service_name": "order-service"})
    ticket_result = await SearchHistoryTicketTool().arun({"service_name": "order-service"})
    cmdb_result = await QueryServiceContextTool().arun({"service_name": "unknown-service"})
    deploy_result = await QueryDeployHistoryTool().arun({"service_name": "order-service"})

    for result in [
        metrics_result,
        alerts_result,
        logs_result,
        traces_result,
        redpanda_result,
        redis_result,
        k8s_result,
        mysql_result,
        ticket_result,
        cmdb_result,
        deploy_result,
    ]:
        assert result.status == "failed"
        assert result.output["status"] == "failed"
        assert result.output["error_type"] == "not_configured"
        assert result.output["signals"] == {}
        assert result.output["raw"] == {}


@pytest.mark.asyncio
async def test_remediation_suggestion_is_marked_rule_based_not_mock() -> None:
    result = await SuggestRemediationTool().arun({"service_name": "order-service"})

    assert result.status == "success"
    assert result.output["source"] == "rule_based"
    assert "need_approval" not in result.output
    assert result.output["change_requires_approval"] is True
    assert result.output["approval_scope"] == "suggested_change_only"


def test_executor_postpones_approval_step_until_read_only_diagnostics_finish() -> None:
    risky_step = PlanStep(
        step_id="s-remediate",
        tool_name="suggest_remediation",
        purpose="suggest remediation",
        input_args={"service_name": "order-service"},
        expected_evidence="remediation",
        risk_level="medium",
    )
    redis_step = PlanStep(
        step_id="s-redis",
        tool_name="query_redis_status",
        purpose="query redis",
        input_args={"service_name": "order-service"},
        expected_evidence="redis status",
        risk_level="low",
    )
    decision = RiskControlDecision(
        action="suggest remediation",
        risk_level="medium",
        policy="approval_required",
        need_approval=True,
        allowed=False,
        reason="needs approval",
        matched_rules=["test"],
    )

    update = _postpone_risky_step_until_read_only_evidence_complete(
        risky_step,
        decision,
        [risky_step.model_dump(mode="json"), redis_step.model_dump(mode="json")],
        "run risky step",
    )

    assert update is not None
    assert update["current_plan"][0]["tool_name"] == "query_redis_status"
    assert update["current_plan"][-1]["tool_name"] == "suggest_remediation"
