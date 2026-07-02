"""Planner degradation tests for external dependency failures."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from app.agent.aiops.state import create_initial_aiops_state
from app.models.plan import PlanStep

planner_module = importlib.import_module("app.agent.aiops.planner")


async def raise_mcp_unavailable() -> Any:
    raise RuntimeError("mcp unavailable")


class FakePlannerPrompt:
    def __or__(self, other: Any) -> FakePlannerChain:
        return FakePlannerChain()


class FakePlannerChain:
    async def ainvoke(self, payload: dict[str, Any]) -> planner_module.Plan:
        return planner_module.Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="query_traces",
                    purpose="LLM plan still runs after MCP discovery failure",
                    input_args={"service_name": "order-service", "lookback": "10m"},
                    expected_evidence="Trace evidence",
                    risk_level="low",
                )
            ]
        )


class FakePlannerLLM:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def with_structured_output(self, model: Any) -> object:
        return object()


@pytest.mark.asyncio
async def test_planner_uses_standard_tools_when_mcp_discovery_fails(monkeypatch) -> None:
    monkeypatch.setattr(planner_module, "planner_prompt", FakePlannerPrompt())

    state = create_initial_aiops_state(
        "order-service 5xx 上升，怀疑下游 Redis timeout",
        session_id="planner-mcp-degraded",
    )

    update = await planner_module.planner(
        state,
        planner_module.PlannerDependencies(
            knowledge_retriever=lambda query: {"status": "no_answer", "content": ""},
            mcp_client_factory=raise_mcp_unavailable,
            llm_factory=FakePlannerLLM,
        ),
    )

    assert update["current_plan"][0]["tool_name"] == "query_traces"
    assert update["current_plan"][0]["purpose"] == "LLM plan still runs after MCP discovery failure"
    assert update["plan"][0].startswith("[s1] 使用 query_traces")
    assert update["warnings"] == [
        "MCP 工具发现失败，Planner 已降级使用本地和标准工具契约继续规划。"
    ]
