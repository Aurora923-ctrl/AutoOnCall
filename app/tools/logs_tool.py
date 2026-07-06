"""Standard log query tool backed by CLS MCP tools with mock fallback."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.config import config
from app.integrations.base import adapter_failure, adapter_not_configured
from app.integrations.log_gateway import HTTPLogGatewayAdapter
from app.integrations.loki import LokiLogAdapter
from app.tools.base import AIOpsTool, clamp_duration, clamp_int, invoke_langchain_tool, tool_map


class QueryLogsTool(AIOpsTool):
    """Query recent service logs through CLS MCP tools or mock logs."""

    name = "query_logs"
    description = "查询服务最近一段时间 ERROR、timeout 或关键异常日志"
    input_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "time_range": {"type": "string", "default": "10m"},
            "query": {"type": "string", "default": "ERROR OR timeout"},
            "limit": {"type": "integer", "default": 100},
        },
        "required": ["service_name"],
    }
    output_schema = {"type": "object"}
    risk_level = "low"
    read_only = True
    timeout_seconds = 15.0
    data_sources = ["Loki", "HTTP log gateway", "CLS MCP", "mock"]
    degradation_strategy = (
        "按 Loki、日志网关、CLS MCP 顺序查询；外部日志不可用时返回 mock 异常日志或结构化不可用结果"
    )

    def __init__(
        self,
        langchain_tools: list[Any] | None = None,
        log_gateway: HTTPLogGatewayAdapter | None = None,
        loki_adapter: LokiLogAdapter | None = None,
    ):
        super().__init__()
        self._allow_adapter_failure_fallback = log_gateway is None and loki_adapter is None
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
        if loki_result and str(loki_result.get("status") or "").lower() != "failed":
            return loki_result

        gateway_result = await self._call_log_gateway(service_name, query, time_range, limit)
        if gateway_result and str(gateway_result.get("status") or "").lower() != "failed":
            return gateway_result
        if (loki_result or gateway_result) and (
            not config.aiops_mock_fallback_enabled or not self._allow_adapter_failure_fallback
        ):
            fallback_result = loki_result or gateway_result
            if fallback_result:
                return fallback_result

        topic_result = await self._search_topic(service_name)
        topic_id = self._extract_topic_id(topic_result)

        if topic_id and "search_log" in self._tools:
            logs_result = await self._search_log(topic_id, query, limit)
            return {
                "service_name": service_name,
                "query": query,
                "source": "mcp_cls",
                "topic": topic_result,
                "logs": logs_result,
                "summary": f"CLS 查询完成，topic_id={topic_id}",
            }

        if not config.aiops_mock_fallback_enabled:
            payload = adapter_not_configured(
                "logs",
                required_config="LOKI_BASE_URL, LOG_GATEWAY_URL, or CLS MCP tools",
                summary_prefix="日志查询不可用",
                service_name=service_name,
                query=query,
                time_range=time_range,
            )
            payload.update(
                {
                    "topic": topic_result,
                    "logs": {"total": 0, "logs": []},
                }
            )
            return payload

        mock_logs = self._mock_logs(service_name)
        return {
            "service_name": service_name,
            "query": query,
            "source": "mock",
            "topic": topic_result,
            "logs": {"total": len(mock_logs), "logs": mock_logs},
            "summary": f"mock 日志发现 {len(mock_logs)} 条异常日志",
        }

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
                summary_prefix="Loki 查询失败",
                service_name=service_name,
                query=query,
            )
            payload.update(
                {
                    "service_name": service_name,
                    "query": query,
                    "logs": {"total": 0, "logs": []},
                }
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
                summary_prefix="日志网关查询失败",
                service_name=service_name,
                query=query,
            )
            payload.update(
                {
                    "service_name": service_name,
                    "query": query,
                    "logs": {"total": 0, "logs": []},
                }
            )
            return payload

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
            return {"total": 0, "topics": [], "error_message": str(exc)}

    async def _search_log(self, topic_id: str, query: str, limit: int) -> Any:
        current_ts = await self._current_timestamp()
        start_ts = current_ts - 10 * 60 * 1000
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

    @staticmethod
    def _mock_logs(service_name: str) -> list[dict[str, Any]]:
        now = datetime.now()
        return [
            {
                "timestamp": (now - timedelta(minutes=3, seconds=12)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "level": "ERROR",
                "message": f"{service_name} Redis connection timeout while calling /api/order/create",
            },
            {
                "timestamp": (now - timedelta(minutes=1, seconds=44)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "level": "ERROR",
                "message": f"{service_name} request failed with 5xx due to downstream cache timeout",
            },
        ]
