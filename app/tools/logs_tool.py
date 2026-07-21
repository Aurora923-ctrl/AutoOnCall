"""Standard log query tool backed by Loki, a log gateway, or CLS MCP tools."""

from __future__ import annotations

from typing import Any

from app.integrations.base import (
    adapter_failure,
    adapter_not_configured,
    public_adapter_failure_message,
)
from app.integrations.log_gateway import HTTPLogGatewayAdapter
from app.integrations.loki import LokiLogAdapter
from app.tools.base import (
    AIOpsTool,
    ToolRetryPolicy,
    clamp_duration,
    clamp_int,
    extract_tool_error_message,
    invoke_langchain_tool,
    is_failed_tool_output,
    tool_map,
)
from app.utils.public_errors import public_exception_message


class QueryLogsTool(AIOpsTool):
    """Query recent service logs through real log adapters."""

    name = "query_logs"
    description = "Query recent ERROR, timeout, or keyword logs for a service."
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "time_range": {"type": "string", "default": "10m"},
            "query": {"type": "string", "default": "ERROR OR timeout"},
            "limit": {"type": "integer", "default": 100},
        },
        "required": ["service_name"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 15.0
    retry_policy = ToolRetryPolicy(
        max_attempts=2,
        backoff_seconds=0.1,
        retry_on=["timeout", "connection_error", "server_error"],
    )
    data_sources = ["Loki", "HTTP log gateway", "CLS MCP"]
    degradation_strategy = (
        "Query Loki, an HTTP log gateway, or CLS MCP tools; return structured unavailable "
        "or failed payloads without synthesizing log evidence."
    )

    def __init__(
        self,
        langchain_tools: list[Any] | None = None,
        log_gateway: HTTPLogGatewayAdapter | None = None,
        loki_adapter: LokiLogAdapter | None = None,
    ):
        super().__init__()
        self._tools = tool_map(langchain_tools)
        self._log_gateway = log_gateway or HTTPLogGatewayAdapter()
        self._loki = loki_adapter or LokiLogAdapter()

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        service_name = input_args.get("service_name") or "unknown-service"
        query = input_args.get("query") or "ERROR OR timeout"
        limit = clamp_int(input_args.get("limit"), default=100, minimum=1, maximum=200)
        time_range = clamp_duration(
            input_args.get("time_range"),
            default="10m",
            maximum_seconds=3600,
        )
        input_args.update({"limit": limit, "time_range": time_range})

        loki_result = await self._call_loki(service_name, query, time_range, limit)
        if loki_result is not None and not is_failed_tool_output(loki_result):
            return loki_result

        gateway_result = await self._call_log_gateway(service_name, query, time_range, limit)
        if gateway_result is not None and not is_failed_tool_output(gateway_result):
            return _with_fallback_errors(
                gateway_result,
                [result for result in (loki_result,) if result],
            )

        cls_result = await self._call_cls(service_name, query, time_range, limit)
        if cls_result is not None:
            return _with_fallback_errors(
                cls_result,
                [result for result in (loki_result, gateway_result) if result],
            )
        if gateway_result is not None:
            return _with_fallback_errors(
                gateway_result,
                [result for result in (loki_result,) if result],
            )
        if loki_result is not None:
            return loki_result

        payload = adapter_not_configured(
            "logs",
            required_config="LOKI_BASE_URL, LOG_GATEWAY_URL, or CLS MCP tools",
            summary_prefix="Log query unavailable",
            service_name=service_name,
            query=query,
            time_range=time_range,
        )
        payload.update({"logs": {"total": 0, "logs": []}})
        return payload

    async def _call_loki(
        self,
        service_name: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> dict[str, Any] | None:
        if not self._loki.configured:
            return None
        try:
            return await self._loki.search_logs(service_name, query, time_range, limit)
        except Exception as exc:
            payload = adapter_failure(
                "loki",
                exc,
                summary_prefix="Loki query failed",
                service_name=service_name,
                query=query,
            )
            payload.update(
                {"service_name": service_name, "query": query, "logs": {"total": 0, "logs": []}}
            )
            return payload

    async def _call_log_gateway(
        self,
        service_name: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> dict[str, Any] | None:
        if not self._log_gateway.configured:
            return None
        try:
            return await self._log_gateway.search_logs(service_name, query, time_range, limit)
        except Exception as exc:
            payload = adapter_failure(
                "log_gateway",
                exc,
                summary_prefix="Log gateway query failed",
                service_name=service_name,
                query=query,
            )
            payload.update(
                {"service_name": service_name, "query": query, "logs": {"total": 0, "logs": []}}
            )
            return payload

    async def _call_cls(
        self,
        service_name: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> dict[str, Any] | None:
        topic_result = await self._search_topic(service_name)
        if is_failed_tool_output(topic_result):
            return {
                "status": "failed",
                "service_name": service_name,
                "query": query,
                "time_range": time_range,
                "source": "mcp_cls",
                "topic": topic_result,
                "logs": {"total": 0, "logs": []},
                "error_message": extract_tool_error_message(topic_result)
                or "CLS topic lookup returned no usable data",
                "summary": "CLS topic lookup failed",
            }
        topic_id = self._extract_topic_id(topic_result)
        if not topic_id or "search_log" not in self._tools:
            return None
        logs_result = await self._search_log(topic_id, query, time_range, limit)
        if is_failed_tool_output(logs_result):
            return {
                "status": "failed",
                "service_name": service_name,
                "query": query,
                "time_range": time_range,
                "source": "mcp_cls",
                "topic": topic_result,
                "logs": logs_result,
                "error_message": extract_tool_error_message(logs_result)
                or "CLS log query returned no usable data",
                "summary": "CLS log query failed",
            }
        synthetic = _is_synthetic_mcp_payload(topic_result) or _is_synthetic_mcp_payload(
            logs_result
        )
        return {
            "service_name": service_name,
            "query": query,
            "time_range": time_range,
            "source": "mock" if synthetic else "mcp_cls",
            "synthetic": synthetic,
            "source_quality": "fallback_only" if synthetic else "live",
            "evidence_origin": "mcp_mock:cls" if synthetic else "mcp:cls",
            "topic": topic_result,
            "logs": logs_result,
            "summary": f"CLS query completed; topic_id={topic_id}",
        }

    async def _search_topic(self, service_name: str) -> Any:
        tool = self._tools.get("search_topic_by_service_name")
        if not tool:
            return {
                "total": 0,
                "topics": [],
                "message": "search_topic_by_service_name MCP tool unavailable",
            }
        try:
            return await invoke_langchain_tool(tool, {"service_name": service_name, "fuzzy": True})
        except Exception as exc:
            return {
                "status": "failed",
                "total": 0,
                "topics": [],
                "error_type": "mcp_error",
                "error_message": public_exception_message(exc),
            }

    async def _search_log(
        self,
        topic_id: str,
        query: str,
        time_range: str,
        limit: int,
    ) -> Any:
        current_ts = await self._current_timestamp()
        window_seconds = _duration_seconds(time_range)
        start_ts = current_ts - window_seconds * 1000
        return await invoke_langchain_tool(
            self._tools["search_log"],
            {
                "topic_id": topic_id,
                "start_time": start_ts,
                "end_time": current_ts,
                "query": query,
                "limit": limit,
            },
        )

    async def _current_timestamp(self) -> int:
        tool = self._tools.get("get_current_timestamp")
        if not tool:
            import time

            return int(time.time() * 1000)
        return int(await invoke_langchain_tool(tool, {}))

    @staticmethod
    def _extract_topic_id(topic_result: Any) -> str | None:
        if isinstance(topic_result, dict):
            topics = topic_result.get("topics") or []
            if topics and isinstance(topics, list) and isinstance(topics[0], dict):
                topic_id = topics[0].get("topic_id")
                return str(topic_id) if topic_id else None
            topic_id = topic_result.get("topic_id")
            return str(topic_id) if topic_id else None
        return None


def _duration_seconds(value: str) -> int:
    text = str(value or "").strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in multipliers:
        return 600
    try:
        amount = int(text[:-1])
    except ValueError:
        return 600
    return max(amount, 1) * multipliers[text[-1]]


def _is_synthetic_mcp_payload(value: Any) -> bool:
    return isinstance(value, dict) and (
        str(value.get("source") or "").lower() == "mock" or value.get("synthetic") is True
    )


def _with_fallback_errors(
    result: dict[str, Any],
    previous: list[dict[str, Any]],
) -> dict[str, Any]:
    if not previous:
        return result
    output = dict(result)
    output["fallback_errors"] = [
        *(output.get("fallback_errors") or []),
        *[
            {
                "source": str(item.get("source") or "unknown"),
                "error_message": public_adapter_failure_message(
                    str(item.get("error_type") or "adapter_error")
                ),
            }
            for item in previous
        ],
    ]
    return output
