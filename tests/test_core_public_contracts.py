"""Compatibility tests for the public core refactoring boundaries."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.agent.aiops.plan_fallback import build_fallback_plan
from app.agent.aiops.replanner import replanner
from app.services.aiops_service import AIOpsService
from app.services.aiops_store import AIOpsStateStore
from app.services.mysql_store import AIOpsMySQLStore
from app.services.rag_agent_service import RagAgentService
from app.services.rag_retrieval_service import retrieve_structured_knowledge
from app.services.report_generator import ReportGenerator
from app.services.sqlite_store import AIOpsSQLiteStore


def _signature_text(callable_object: Callable[..., Any]) -> str:
    return str(inspect.signature(callable_object))


def test_core_public_entrypoint_signatures_remain_compatible() -> None:
    assert _signature_text(retrieve_structured_knowledge) == (
        "(query: 'str', *, top_k: 'int | None' = None, max_distance: 'float | None' = None, "
        "metadata_filter: 'dict[str, Any] | None' = None, "
        "hybrid_search_enabled: 'bool | None' = None, rerank_enabled: 'bool | None' = None, "
        "fusion_strategy: 'str | None' = None, vector_store: 'Any | None' = None, "
        "vector_store_provider: 'Callable[[], Any] | None' = None, "
        "lexical_index: 'Any | None' = None) -> 'dict[str, Any]'"
    )
    assert _signature_text(ReportGenerator) == (
        "(storage_path: 'str | Path | None' = None, *, "
        "legacy_storage_path: 'str | Path | None' = None)"
    )
    assert _signature_text(ReportGenerator.generate_from_state) == (
        "(self, state: 'dict[str, Any]', *, trace_events: 'list[TraceEvent] | None' = None, "
        "status: 'str' = 'completed') -> 'DiagnosisReport'"
    )
    assert _signature_text(RagAgentService) == "(streaming: bool = True)"
    assert _signature_text(RagAgentService.query_with_retrieval) == (
        "(self, question: str, session_id: str, metadata_filter: dict[str, typing.Any] | None = "
        "None, *, include_evaluation_context: bool = False) -> dict[str, typing.Any]"
    )
    assert _signature_text(AIOpsService) == "()"
    assert _signature_text(AIOpsService.execute) == (
        "(self, user_input: str, session_id: str | None = None, "
        "incident: app.models.incident.Incident | None = None) -> "
        "collections.abc.AsyncGenerator[dict[str, typing.Any], None]"
    )
    assert _signature_text(AIOpsService.resume_after_approval) == (
        "(self, *, session_id: str, incident_id: str, "
        "approval: app.models.approval.ApprovalRequest) -> "
        "collections.abc.AsyncGenerator[dict[str, typing.Any], None]"
    )
    assert _signature_text(replanner) == (
        "(state: app.agent.aiops.state.PlanExecuteState) -> dict[str, typing.Any]"
    )
    assert _signature_text(build_fallback_plan) == (
        "(input_text: 'str', incident: 'dict[str, Any] | None' = None) -> 'list[PlanStep]'"
    )


def _parameter_contract(callable_object: Callable[..., Any]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (parameter.name, parameter.kind, parameter.default, str(parameter.annotation))
        for parameter in inspect.signature(callable_object).parameters.values()
    )


def test_sqlite_and_mysql_implement_every_store_protocol_method_with_matching_parameters() -> None:
    protocol_methods = {
        name: value
        for name, value in AIOpsStateStore.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert protocol_methods
    for name, protocol_method in protocol_methods.items():
        sqlite_method = getattr(AIOpsSQLiteStore, name)
        mysql_method = getattr(AIOpsMySQLStore, name)
        expected = _parameter_contract(protocol_method)

        assert _parameter_contract(sqlite_method) == expected, f"SQLite signature drift: {name}"
        assert _parameter_contract(mysql_method) == expected, f"MySQL signature drift: {name}"


@pytest.mark.parametrize(
    ("scenario", "input_text", "incident", "expected_tools"),
    [
        (
            "redis",
            "order-service Redis maxclients timeout",
            {"service_name": "order-service", "symptom": "Redis maxclients timeout"},
            ["query_service_context", "query_redis_status", "query_metrics", "query_logs"],
        ),
        (
            "mysql",
            "billing-service MySQL slow query pool exhausted",
            {"service_name": "billing-service", "symptom": "MySQL slow query pool exhausted"},
            ["query_service_context", "query_metrics", "query_logs", "query_mysql_status"],
        ),
        (
            "crashloop",
            "catalog-service Pod CrashLoopBackOff OOMKilled",
            {"service_name": "catalog-service", "symptom": "Pod CrashLoopBackOff OOMKilled"},
            ["query_service_context", "query_k8s_status", "query_logs", "query_metrics"],
        ),
        (
            "generic",
            "checkout-service has an unknown incident",
            {"service_name": "checkout-service", "symptom": "unknown incident"},
            ["query_service_context", "query_metrics", "query_logs", "query_deploy_history"],
        ),
    ],
)
def test_fallback_plan_keeps_scenario_specific_evidence_order(
    scenario: str,
    input_text: str,
    incident: dict[str, Any],
    expected_tools: list[str],
) -> None:
    steps = build_fallback_plan(input_text, incident)

    assert [step.tool_name for step in steps[: len(expected_tools)]] == expected_tools, scenario
    assert all(step.status == "pending" for step in steps)
    assert [step.step_id for step in steps] == [
        f"s{index}" for index in range(1, len(steps) + 1)
    ]


def test_report_generator_public_repository_methods_keep_latest_report_semantics(
    tmp_path: Path,
) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")

    assert generator.get_report("missing-incident") is None
    assert generator.list_reports() == []
