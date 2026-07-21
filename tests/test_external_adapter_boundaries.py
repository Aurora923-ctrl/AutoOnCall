"""Failure-envelope and query-boundary tests for external adapters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.integrations.base import (
    ExternalAdapterError,
    ExternalAdapterNotFoundError,
    ExternalAdapterResponseError,
    classify_adapter_error,
    require_success_payload,
)
from app.integrations.kubernetes import KubernetesStatusAdapter
from app.integrations.log_gateway import HTTPLogGatewayAdapter
from app.integrations.loki import LokiLogAdapter
from app.integrations.prometheus import PrometheusMetricsAdapter
from app.integrations.redis_info import RedisInfoAdapter, _redis_operation_deadline
from app.integrations.service_catalog import CMDBAdapter, DeployHistoryAdapter
from app.integrations.ticketing import TicketingAdapter


def _response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_business_failure_payload_is_classified_as_retryable_server_error() -> None:
    with pytest.raises(ExternalAdapterResponseError) as exc_info:
        require_success_payload(
            {"status": "success", "success": False, "message": "database host is secret"},
            system_name="CMDB",
        )

    assert classify_adapter_error(exc_info.value) == "server_error"
    assert "database host is secret" not in str(exc_info.value)


def test_business_error_fields_are_rejected_even_without_failed_status() -> None:
    with pytest.raises(ExternalAdapterResponseError, match="error payload"):
        require_success_payload(
            {"status": "success", "error_message": "mysql://user:secret@internal"},
            system_name="CMDB",
        )


@pytest.mark.asyncio
async def test_prometheus_uses_requested_window_and_rejects_business_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        "app.integrations.prometheus.config.prometheus_qps_query",
        'sum(rate(http_requests_total{service="{service_name}"}[5m]))',
    )

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        assert request.url.path == "/api/v1/query_range"
        assert request.url.params["step"] == "60"
        if len(queries) == 1:
            return _response({"status": "error", "error": "token=secret"})
        return _response(
            {
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            }
        )

    adapter = PrometheusMetricsAdapter(
        base_url="http://prometheus",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ExternalAdapterResponseError, match="failed status"):
        await adapter.query_service_metrics("order-service", time_range="30m")

    assert "[1800s]" in queries[0]


@pytest.mark.asyncio
async def test_prometheus_treats_non_finite_values_as_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _response(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"values": [[1_700_000_000, "NaN"]]}],
                },
            }
        )

    payload = await PrometheusMetricsAdapter(
        base_url="http://prometheus",
        transport=httpx.MockTransport(handler),
    ).query_service_metrics("order-service")

    assert payload["status"] == "failed"
    assert payload["error_type"] == "no_data"
    assert set(payload["empty_queries"]) == {
        "qps",
        "error_rate",
        "p95_latency_ms",
        "cpu_usage_percent",
        "memory_working_set_bytes",
    }


@pytest.mark.asyncio
async def test_prometheus_uses_one_total_budget_across_all_queries() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.015)
        return _response(
            {
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            }
        )

    adapter = PrometheusMetricsAdapter(
        base_url="http://prometheus",
        timeout_seconds=0.025,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TimeoutError, match="budget"):
        await adapter.query_service_metrics("order-service")

    assert calls < 5


def test_redis_stream_resp_parser_preserves_nested_entries() -> None:
    payload = [
        [
            "1783000000000-0",
            [
                "event",
                "redis_timeout",
                "detail",
                "connection pool exhausted",
                "ts",
                "1783000000000",
            ],
        ],
        ["1782999999000-0", ["event", "alert", "detail", "5xx spike"]],
    ]

    assert RedisInfoAdapter._parse_stream_entries(payload) == [
        {
            "id": "1783000000000-0",
            "event": "redis_timeout",
            "detail": "connection pool exhausted",
            "ts": "1783000000000",
        },
        {
            "id": "1782999999000-0",
            "event": "alert",
            "detail": "5xx spike",
        },
    ]


def test_redis_replay_evidence_is_filtered_by_requested_window() -> None:
    now = datetime.now(UTC)
    stale = {"updated_at": (now - timedelta(hours=2)).isoformat(), "connected_clients": "9940"}
    fresh = {"updated_at": (now - timedelta(minutes=5)).isoformat(), "connected_clients": "9940"}
    window_started_at = now - timedelta(minutes=10)

    assert (
        RedisInfoAdapter._evidence_within_window(
            stale,
            window_started_at=window_started_at,
        )
        == {}
    )
    assert (
        RedisInfoAdapter._evidence_within_window(
            fresh,
            window_started_at=window_started_at,
        )
        == fresh
    )


def test_redis_unknown_named_instance_does_not_fall_back_to_default() -> None:
    adapter = RedisInfoAdapter()
    adapter.redis_url = "redis://default-redis:6379/0"
    adapter.instance_urls = {"redis-cluster-prod": "redis://prod-redis:6379/0"}

    with pytest.raises(ExternalAdapterNotFoundError, match="redis-cluster-typo"):
        adapter._resolve_target("redis-cluster-typo")


@pytest.mark.asyncio
async def test_redis_resp_rejects_oversized_bulk_before_reading_body() -> None:
    adapter = RedisInfoAdapter()
    reader = asyncio.StreamReader()
    reader.feed_data(f"${adapter.MAX_RESP_BULK_BYTES + 1}\r\n".encode())
    reader.feed_eof()

    with pytest.raises(ExternalAdapterError, match="bulk response exceeds"):
        await adapter._read_resp(
            reader,
            depth=0,
            remaining_bytes=[adapter.MAX_RESP_TOTAL_BYTES],
        )


@pytest.mark.asyncio
async def test_redis_resp_rejects_excessive_nesting() -> None:
    adapter = RedisInfoAdapter()
    reader = asyncio.StreamReader()
    reader.feed_data(b"*1\r\n" * (adapter.MAX_RESP_DEPTH + 2) + b"+OK\r\n")
    reader.feed_eof()

    with pytest.raises(ExternalAdapterError, match="nesting limit"):
        await adapter._read_resp(
            reader,
            depth=0,
            remaining_bytes=[adapter.MAX_RESP_TOTAL_BYTES],
        )


@pytest.mark.asyncio
async def test_redis_commands_share_one_operation_deadline() -> None:
    adapter = RedisInfoAdapter()
    adapter.timeout_seconds = 0.1
    reader = asyncio.StreamReader()

    async def delayed_drain() -> None:
        await asyncio.sleep(0.06)

    class Writer:
        def write(self, _: bytes) -> None:
            return None

        async def drain(self) -> None:
            await delayed_drain()

    reader.feed_data(b"+OK\r\n+OK\r\n")
    reader.feed_eof()
    token = _redis_operation_deadline.set(asyncio.get_running_loop().time() + 0.1)
    try:
        assert await adapter._send_command(reader, Writer(), "PING") == "OK"
        with pytest.raises(TimeoutError):
            await adapter._send_command(reader, Writer(), "PING")
    finally:
        _redis_operation_deadline.reset(token)


def test_redis_named_instance_requires_explicit_instance_map() -> None:
    adapter = RedisInfoAdapter()
    adapter.redis_url = "redis://default-redis:6379/0"
    adapter.instance_urls = {}

    with pytest.raises(ExternalAdapterNotFoundError, match="redis-cluster-prod"):
        adapter._resolve_target("redis-cluster-prod")


def test_redis_optional_admin_errors_are_publicized() -> None:
    error = RedisInfoAdapter._optional_command_error(
        "CONFIG GET maxclients",
        PermissionError("ACL denied on redis.internal"),
    )

    assert error["error_type"] == "permission_denied"
    assert "redis.internal" not in error["error_message"]


def test_not_found_adapter_error_has_stable_classification() -> None:
    assert (
        classify_adapter_error(ExternalAdapterNotFoundError("internal instance name"))
        == "not_found"
    )


@pytest.mark.asyncio
async def test_loki_rejects_success_payload_without_result_array() -> None:
    adapter = LokiLogAdapter(
        base_url="http://loki",
        transport=httpx.MockTransport(
            lambda _request: _response({"status": "success", "data": {}})
        ),
    )

    with pytest.raises(ExternalAdapterResponseError, match=r"data\.result"):
        await adapter.search_logs("order-service", "ERROR", "10m", 20)


@pytest.mark.asyncio
async def test_log_gateway_rejects_success_payload_with_non_list_logs() -> None:
    adapter = HTTPLogGatewayAdapter(
        url="http://logs",
        transport=httpx.MockTransport(
            lambda _request: _response({"status": "success", "logs": {"message": "bad"}})
        ),
    )

    with pytest.raises(ExternalAdapterResponseError, match="must be an array"):
        await adapter.search_logs("order-service", "ERROR", "10m", 20)


@pytest.mark.asyncio
async def test_kubernetes_event_partial_failure_is_classified_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.integrations.kubernetes.config.kubernetes_api_server",
        "https://kubernetes",
    )
    monkeypatch.setattr("app.integrations.kubernetes.config.kubernetes_namespace", "prod")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pods"):
            return _response(
                {
                    "items": [
                        {
                            "metadata": {"name": "order-service-abc"},
                            "status": {
                                "phase": "Running",
                                "hostIP": "10.0.0.8",
                                "containerStatuses": [{"ready": True, "restartCount": 0}],
                            },
                        }
                    ]
                }
            )
        return _response({"message": "Authorization: Bearer secret-token"}, status_code=403)

    payload = await KubernetesStatusAdapter(
        transport=httpx.MockTransport(handler)
    ).query_service_status("order-service")

    assert payload["status"] == "success"
    assert payload["partial_errors"] == [
        {
            "query": "events",
            "error_type": "permission_denied",
            "error_message": "外部依赖权限校验失败",
        }
    ]
    assert "10.0.0.8" not in str(payload)
    assert "secret-token" not in str(payload)


@pytest.mark.asyncio
async def test_kubernetes_rejects_namespace_path_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.integrations.kubernetes.config.kubernetes_api_server",
        "https://kubernetes",
    )
    monkeypatch.setattr(
        "app.integrations.kubernetes.config.kubernetes_namespace",
        "../kube-system",
    )
    adapter = KubernetesStatusAdapter(
        transport=httpx.MockTransport(lambda _request: _response({"items": []}))
    )

    with pytest.raises(ExternalAdapterError, match="namespace"):
        await adapter.query_service_status("order-service")


@pytest.mark.asyncio
async def test_business_adapter_percent_encodes_service_path_segment() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return _response(
            {
                "service": {
                    "owner": "team",
                    "dependencies": [],
                }
            }
        )

    await CMDBAdapter(
        url="http://cmdb",
        transport=httpx.MockTransport(handler),
    ).query_service("../admin")

    assert paths == ["/services/..%2Fadmin.json"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_call",
    [
        lambda transport: CMDBAdapter(url="http://cmdb", transport=transport).query_service(
            "order-service"
        ),
        lambda transport: DeployHistoryAdapter(
            url="http://deploy", transport=transport
        ).query_deployments("order-service"),
        lambda transport: TicketingAdapter(
            url="http://tickets", transport=transport
        ).search_history("order-service"),
    ],
)
async def test_business_adapters_reject_http_200_failure_envelopes(adapter_call) -> None:
    transport = httpx.MockTransport(
        lambda _request: _response(
            {"status": "failed", "error_message": "mysql://user:password@internal"}
        )
    )

    with pytest.raises(ExternalAdapterResponseError):
        await adapter_call(transport)
