"""Offline AIOps evaluation runner for deterministic incident cases."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _preload_env_file_from_argv() -> None:
    """Load --env-file before importing app.config-backed modules."""
    env_file = ""
    for index, arg in enumerate(sys.argv):
        if arg == "--env-file" and index + 1 < len(sys.argv):
            env_file = sys.argv[index + 1]
            break
        if arg.startswith("--env-file="):
            env_file = arg.split("=", 1)[1]
            break
    if not env_file:
        return
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = REPO_ROOT / env_path
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


_preload_env_file_from_argv()

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import analyze_evidence
from app.agent.aiops.executor import (
    _tool_result_to_call_record as executor_tool_result_to_call_record,
    _tool_result_to_evidence as executor_tool_result_to_evidence,
)
from app.agent.aiops.plan_fallback import build_fallback_plan
from app.agent.aiops.risk_controller import assess_plan_step
from app.config import config
from app.models.evidence import Evidence
from app.models.incident import Incident
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.services.alert_ingestion_service import _build_incident, _normalize_alertmanager_alert
from app.services.report_generator import ReportGenerator
from app.tools.base import AIOpsTool, ToolExecutionResult
from app.tools.logs_tool import QueryLogsTool
from app.tools.metrics_tool import QueryMetricsTool
from app.tools.ops_tool import (
    QueryK8sStatusTool,
    QueryMySQLStatusTool,
    SearchHistoryTicketTool,
    SuggestRemediationTool,
)
from app.tools.redis_tool import QueryRedisStatusTool
from app.tools.registry import ToolRegistry
from scripts.eval.eval_environment import collect_eval_environment
from scripts.eval.eval_rag_cases import evaluate_cases as evaluate_rag_cases

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "cases.yaml"
DEFAULT_REPORT_PATH = REPO_ROOT / "logs" / "eval_reports.db"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "eval_summary.md"
DEFAULT_RAG_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_RAG_DOCS_DIR = REPO_ROOT / "aiops-docs"
DEFAULT_ENV_FILE = REPO_ROOT / "deploy" / "sandbox.env"

METRIC_NAMES = [
    "tool_hit",
    "tool_sequence_hit",
    "executed_tool_hit",
    "forbidden_tools_avoided",
    "root_cause_hit",
    "risk_policy_hit",
    "approval_hit",
    "report_generated",
    "report_status_hit",
    "report_contains_evidence",
    "evidence_count_hit",
    "confidence_hit",
    "runbook_rejection_hit",
    "tool_failure_graceful_degradation",
    "tool_selection_recall",
    "unnecessary_tool_rate",
    "hypothesis_ranking_hit",
    "evidence_support_rate",
    "approval_recall",
    "forbidden_precision",
    "degradation_success",
    "trace_completeness",
    "alertmanager_payload_hit",
    "incident_field_hit",
    "evidence_fields_hit",
    "golden_signal_hit",
    "required_live_sources_hit",
    "report_structure_hit",
    "evidence_sufficiency_hit",
    "runtime_vs_incident_boundary_hit",
    "approval_boundary_hit",
]

METRIC_FAILURE_REASONS = {
    "tool_hit": "Planner 未包含 case 期望的诊断工具。",
    "tool_sequence_hit": "Planner 工具顺序前缀与 case 期望不一致。",
    "executed_tool_hit": "实际执行工具未覆盖 case 期望工具。",
    "forbidden_tools_avoided": "计划或执行链路中出现了禁止自动执行的工具。",
    "root_cause_hit": "报告根因或证据分析未命中期望根因关键词。",
    "risk_policy_hit": "风险策略与 case 期望不一致。",
    "approval_hit": "审批触发结果与 case 期望不一致。",
    "report_generated": "没有生成包含 report_id、markdown 和 root_cause 的报告。",
    "report_status_hit": "报告状态与 case 期望不一致。",
    "report_contains_evidence": "报告正文未包含 case 要求的证据关键词。",
    "evidence_count_hit": "证据数量低于 case 要求。",
    "confidence_hit": "报告置信度低于 case 要求。",
    "runbook_rejection_hit": "Runbook 无答案拒答结果与 case 期望不一致。",
    "tool_failure_graceful_degradation": "工具失败后没有降级生成可用报告或失败工具未被记录。",
    "tool_selection_recall": "Planner 工具选择没有覆盖期望诊断工具。",
    "unnecessary_tool_rate": "Planner 选择了过多与 case 无关的工具。",
    "hypothesis_ranking_hit": "根因假设排序未命中期望根因关键词。",
    "evidence_support_rate": "支持性证据比例不足，诊断链路解释性较弱。",
    "approval_recall": "需要审批的 case 未进入审批策略。",
    "forbidden_precision": "禁止动作 case 未被稳定识别为 forbidden。",
    "degradation_success": "工具失败场景没有生成可用降级报告。",
    "trace_completeness": "离线诊断链路缺少 trace_id、工具调用或证据闭环。",
    "evidence_sufficiency_hit": "报告没有按主故障域、现象侧、处置参考和失败工具执行证据充分性门槛。",
    "runtime_vs_incident_boundary_hit": "报告或工具输出未说明 runtime 与 incident evidence 的边界。",
    "approval_boundary_hit": "报告或评测摘要未明确诊断不需要审批、处置变更需要审批的边界。",
}


class EvalRunbookTool(AIOpsTool):
    """Offline runbook tool used to keep evals independent of Milvus/RAG."""

    name = "search_runbook"
    description = "离线检索内置评测 Runbook"
    risk_level = "low"
    read_only = True

    def __init__(self, *, should_reject: bool = False):
        self._should_reject = should_reject

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        query = str(input_args.get("query") or "")
        if self._should_reject:
            return {
                "query": query,
                "source": "eval_fixture",
                "retrieval_results": [],
                "no_answer_rejected": True,
                "summary": "未找到可信知识来源，已触发 Runbook 无答案拒答",
            }

        retrieval_results = _eval_runbook_sources(query)
        return {
            "query": query,
            "source": "eval_fixture",
            "content": _eval_runbook_content(query, retrieval_results),
            "retrieval_results": retrieval_results,
            "no_answer_rejected": False,
            "summary": "离线 Runbook fixture 命中",
        }


def _eval_runbook_sources(query: str) -> list[dict[str, Any]]:
    lowered = query.lower()
    if "redis" in lowered or "maxclients" in lowered:
        return [
            {
                "source_file": "redis_postmortem.pdf",
                "heading_path": "Redis Maxclients Postmortem",
                "chunk_id": "redis_postmortem.pdf#page-1",
                "score": 1.0,
                "metadata": {"doc_type": "pdf", "page_number": 1},
            },
            {
                "source_file": "tickets.csv",
                "heading_path": "tickets.csv row ticket_id=INC-REDIS-001",
                "chunk_id": "tickets.csv#row-2",
                "score": 0.95,
                "metadata": {
                    "doc_type": "table",
                    "sheet_name": "csv",
                    "row_number": 2,
                    "primary_key": "ticket_id=INC-REDIS-001",
                },
            },
        ]
    if "mysql" in lowered or "slow query" in lowered or "payment" in lowered:
        return [
            {
                "source_file": "payment_wiki.html",
                "heading_path": "Payment Runbook > MySQL 慢查询",
                "chunk_id": "payment_wiki.html#mysql-slow-query",
                "score": 1.0,
                "metadata": {"doc_type": "html"},
            },
            {
                "source_file": "tickets.xlsx",
                "heading_path": "tickets.xlsx deploy_history row payment-service",
                "chunk_id": "tickets.xlsx#deploy_history-row-2",
                "score": 0.94,
                "metadata": {
                    "doc_type": "table",
                    "sheet_name": "deploy_history",
                    "row_number": 2,
                    "primary_key": "service_name=payment-service",
                },
            },
        ]
    return [
        {
            "source_file": "eval_runbook.md",
            "heading_path": "AIOps Eval Fixture",
            "chunk_id": "eval-runbook-001",
            "score": 1.0,
        }
    ]


def _eval_runbook_content(query: str, retrieval_results: list[dict[str, Any]]) -> str:
    sources = ", ".join(str(item.get("source_file")) for item in retrieval_results)
    return f"Runbook fixture for: {query}; cited_sources={sources}"


class EvalFailureTool(AIOpsTool):
    """Tool wrapper that injects deterministic failures for eval cases."""

    def __init__(self, wrapped: AIOpsTool, failure: dict[str, Any]):
        self.name = wrapped.name
        self.description = wrapped.description
        self.input_schema = wrapped.input_schema
        self.output_schema = wrapped.output_schema
        self.risk_level = wrapped.risk_level
        self.read_only = wrapped.read_only
        self.timeout_seconds = wrapped.timeout_seconds
        self._error_type = str(failure.get("error_type") or "eval_injected_failure")
        self._message = str(
            failure.get("error_message") or failure.get("message") or "评测注入失败"
        )

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "failed",
            "source": "eval_failure",
            "error_type": self._error_type,
            "error_message": self._message,
            "summary": f"{self.name} 模拟失败: {self._message}",
        }


class EvalStaticOutputTool(AIOpsTool):
    """Tool wrapper that returns a deterministic fixture payload."""

    def __init__(self, wrapped: AIOpsTool, output: dict[str, Any]):
        self.name = wrapped.name
        self.description = wrapped.description
        self.input_schema = wrapped.input_schema
        self.output_schema = wrapped.output_schema
        self.risk_level = wrapped.risk_level
        self.read_only = wrapped.read_only
        self.timeout_seconds = wrapped.timeout_seconds
        self._output = dict(output)

    async def _call(self, input_args: dict[str, Any]) -> dict[str, Any]:
        output = dict(self._output)
        output.setdefault("source", "eval_fixture")
        output.setdefault("summary", f"{self.name} eval fixture returned")
        return output


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load case definitions from YAML."""
    case_path = Path(path)
    payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No eval cases found in {case_path}")
    return [dict(case) for case in cases]


def load_env_file(path: str | Path | None, *, override: bool = False) -> None:
    """Load simple KEY=VALUE env files before creating live adapters."""
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if override or key not in os.environ:
            os.environ[key] = value
        _sync_loaded_env_to_config(key, value)


def _sync_loaded_env_to_config(key: str, value: str) -> None:
    """Keep programmatic eval calls aligned with CLI pre-import env loading."""
    config_key = key.strip().lower()
    field_name = config_key
    if not hasattr(config, field_name):
        return
    current = getattr(config, field_name)
    try:
        if isinstance(current, bool):
            parsed: Any = value.strip().lower() in {"1", "true", "yes", "on"}
        elif isinstance(current, int) and not isinstance(current, bool):
            parsed = int(value)
        elif isinstance(current, float):
            parsed = float(value)
        else:
            parsed = value
        setattr(config, field_name, parsed)
    except (TypeError, ValueError):
        setattr(config, field_name, value)


async def evaluate_cases(
    cases_path: str | Path = DEFAULT_CASES_PATH,
    *,
    report_path: str | Path | None = None,
    include_rag: bool = True,
    rag_cases_path: str | Path = DEFAULT_RAG_CASES_PATH,
    rag_docs_dir: str | Path = DEFAULT_RAG_DOCS_DIR,
) -> dict[str, Any]:
    """Evaluate all cases and return aggregate metrics."""
    started_at = datetime.now(UTC)
    started_timer = time.perf_counter()
    cases = load_cases(cases_path)
    generator = ReportGenerator(report_path or DEFAULT_REPORT_PATH)
    results = [await evaluate_case_safely(case, generator) for case in cases]
    rag_payload = evaluate_rag_cases(rag_cases_path, docs_dir=rag_docs_dir) if include_rag else None
    ended_at = datetime.now(UTC)
    summary = build_summary(results, rag_payload=rag_payload)
    payload = {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic regression; golden Redis/MySQL cases use live configured "
                "Docker adapters when REDIS/MYSQL settings are present, other AIOps tools use "
                "deterministic fixtures, and RAG uses local lexical retrieval, not live LLM"
            ),
            "cases_path": str(Path(cases_path)),
            "report_path": str(report_path or DEFAULT_REPORT_PATH),
            "rag_cases_path": str(Path(rag_cases_path)) if include_rag else "",
            "rag_docs_dir": str(Path(rag_docs_dir)) if include_rag else "",
            "case_ids": [str(case.get("id", "")) for case in cases],
            "environment": collect_eval_environment(suite="aiops"),
        },
        "summary": summary,
        "cases": results,
    }
    if rag_payload is not None:
        payload["rag"] = rag_payload
    return payload


async def evaluate_case_safely(
    case: dict[str, Any],
    generator: ReportGenerator,
) -> dict[str, Any]:
    """Evaluate one case and convert unexpected exceptions into failed case records."""
    started = time.perf_counter()
    try:
        result = await evaluate_case(case, generator)
    except Exception as exc:  # pragma: no cover - defensive path for malformed eval cases
        result = build_exception_result(case, exc)
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def evaluate_case(case: dict[str, Any], generator: ReportGenerator) -> dict[str, Any]:
    """Evaluate one deterministic incident case."""
    incident = build_eval_incident(case)
    state = create_initial_aiops_state(
        str(case.get("input") or incident.symptom),
        session_id=f"eval-{case['id']}",
        incident=incident,
    )
    plan_steps = build_fallback_plan(state["input"], state["incident"])
    registry = create_eval_tool_registry(case)
    risk_decisions = [
        assess_plan_step(step, tool_registry=registry, incident=state["incident"])
        for step in plan_steps
    ]

    if case.get("risk_step"):
        risk_decisions.append(
            assess_plan_step(
                PlanStep(**case["risk_step"]),
                tool_registry=registry,
                incident=state["incident"],
            )
        )

    evidence, tool_calls = await execute_safe_eval_steps(
        plan_steps,
        registry,
        trace_id=state["trace_id"],
        incident_id=state["incident"]["incident_id"],
    )
    state["current_plan"] = []
    state["plan"] = []
    state["gathered_evidence"] = [item.model_dump(mode="json") for item in evidence]
    state["tool_call_records"] = [item.model_dump(mode="json") for item in tool_calls]

    analysis = analyze_evidence(state)
    state["hypotheses"] = analysis.hypotheses
    state["final_diagnosis"] = analysis.hypotheses[0] if analysis.hypotheses else ""
    risk_policy = strongest_policy(risk_decisions)
    if risk_policy != "allow":
        decision = next(item for item in risk_decisions if item.policy == risk_policy)
        state["risk_assessment"] = decision.to_risk_assessment().model_dump(mode="json")

    report_status = "blocked" if risk_policy == "forbidden" else "completed"
    if risk_policy == "approval_required":
        report_status = "waiting_approval"
    report = generator.generate_from_state(state, trace_events=[], status=report_status)

    planned_tools = [step.tool_name for step in plan_steps]
    executed_tools = [record.tool_name for record in tool_calls]
    failed_tools = [record.tool_name for record in tool_calls if record.status == "failed"]
    expected_executed_tools = case.get("expected_executed_tools", case.get("expected_tools", []))
    expected_failed_tools = case.get("expected_failed_tools", [])
    forbidden_tools = case.get("forbidden_tools", [])
    expected_status = str(case.get("expected_report_status") or report_status)
    min_evidence_count = int(case.get("min_evidence_count") or len(expected_executed_tools) or 1)
    min_confidence = float(case.get("min_confidence", 0.65))
    report_generated = bool(report.report_id and report.markdown and report.root_cause)
    expected_runbook_rejection = bool(case.get("runbook_should_reject", False))
    observed_runbook_rejection = runbook_rejected(tool_calls)
    unexpected_tool_rate = diagnostic_unnecessary_tool_rate(
        planned_tools,
        case.get("expected_tools", []),
    )
    support_rate = ratio(
        len([item for item in evidence if item.stance == "supporting"]),
        len(evidence),
    )
    hypothesis_text = "\n".join(
        [
            report.root_cause,
            report.markdown,
            *analysis.hypotheses,
            *[item.title for item in analysis.hypothesis_ranking],
        ]
    )
    trace_complete = bool(
        report.trace_id
        and tool_calls
        and evidence
        and all(record.status in {"success", "failed"} for record in tool_calls)
    )
    golden = golden_expectations(case)

    metrics = {
        "tool_hit": contains_all(planned_tools, case.get("expected_tools", [])),
        "tool_sequence_hit": expected_tool_order_hit(
            planned_tools,
            case.get("expected_tool_order_prefix", []),
            strict=bool(golden),
        ),
        "executed_tool_hit": contains_all(executed_tools, expected_executed_tools),
        "forbidden_tools_avoided": has_no_overlap(
            planned_tools + executed_tools,
            forbidden_tools,
        ),
        "root_cause_hit": text_has_all(
            "\n".join([report.root_cause, report.markdown, *analysis.hypotheses]),
            case.get("expected_root_keywords", []),
        ),
        "risk_policy_hit": risk_policy == case.get("expected_risk_policy", "allow"),
        "approval_hit": any(decision.need_approval for decision in risk_decisions)
        == bool(case.get("expected_needs_approval", False)),
        "report_generated": report_generated,
        "report_status_hit": report.status == expected_status,
        "report_contains_evidence": text_has_all(
            report.markdown, case.get("report_must_contain", [])
        ),
        "evidence_count_hit": len(evidence) >= min_evidence_count,
        "confidence_hit": report.confidence >= min_confidence,
        "runbook_rejection_hit": (
            observed_runbook_rejection if expected_runbook_rejection else True
        ),
        "tool_failure_graceful_degradation": (
            contains_all(failed_tools, expected_failed_tools) and report_generated
            if expected_failed_tools
            else True
        ),
        "tool_selection_recall": contains_all(planned_tools, case.get("expected_tools", [])),
        "unnecessary_tool_rate": unexpected_tool_rate
        <= float(case.get("max_unnecessary_tool_rate", 0.4)),
        "hypothesis_ranking_hit": text_has_all(
            hypothesis_text,
            case.get("expected_root_keywords", []),
        ),
        "evidence_support_rate": support_rate >= float(case.get("min_evidence_support_rate", 0.4)),
        "approval_recall": (
            any(decision.need_approval for decision in risk_decisions)
            if case.get("expected_needs_approval", False)
            else True
        ),
        "forbidden_precision": (
            risk_policy == "forbidden" if case.get("expected_risk_policy") == "forbidden" else True
        ),
        "degradation_success": (
            contains_all(failed_tools, expected_failed_tools) and report_generated
            if expected_failed_tools
            else True
        ),
        "trace_completeness": trace_complete,
        "alertmanager_payload_hit": alertmanager_payload_hit(case),
        "incident_field_hit": incident_field_hit(case, state["incident"]),
        "evidence_fields_hit": evidence_fields_hit(evidence, golden),
        "golden_signal_hit": golden_signal_hit(tool_calls, golden),
        "required_live_sources_hit": required_live_sources_hit(tool_calls, golden, case=case),
        "report_structure_hit": report_structure_hit(report, golden),
        "evidence_sufficiency_hit": evidence_sufficiency_hit(report, golden),
        "runtime_vs_incident_boundary_hit": runtime_vs_incident_boundary_hit(
            report,
            tool_calls,
            golden,
        ),
        "approval_boundary_hit": approval_boundary_hit(report, golden, risk_policy),
    }
    failed_metrics = failed_metric_names(metrics)
    return {
        "id": case["id"],
        "passed": not failed_metrics,
        "metrics": metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": failure_reasons(failed_metrics),
        "planned_tools": planned_tools,
        "executed_tools": executed_tools,
        "failed_tools": failed_tools,
        "expected_failed_tools": expected_failed_tools,
        "forbidden_tools": forbidden_tools,
        "risk_policy": risk_policy,
        "expected_risk_policy": case.get("expected_risk_policy", "allow"),
        "expected_needs_approval": bool(case.get("expected_needs_approval", False)),
        "report_status": report.status,
        "hypotheses": analysis.hypotheses,
        "hypothesis_ranking": [
            item.model_dump(mode="json") for item in analysis.hypothesis_ranking
        ],
        "evidence_count": len(evidence),
        "evidence_support_rate": support_rate,
        "unnecessary_tool_rate": unexpected_tool_rate,
        "report_id": report.report_id,
        "confidence": report.confidence,
        "conclusion_alignment": report.conclusion_alignment,
        "runbook_rejected": observed_runbook_rejection,
        "runbook_should_reject": expected_runbook_rejection,
        "tool_latency_ms": [record.latency_ms for record in tool_calls],
        "tool_sources": {
            record.tool_name: record.output.get("source", "")
            for record in tool_calls
            if isinstance(record.output, dict)
        },
        "evidence_mode": golden_evidence_mode(golden),
        "source_boundary": golden.get("source_boundary", ""),
        "golden_chain": build_golden_chain_summary(
            case=case,
            state=state,
            plan_steps=plan_steps,
            evidence=evidence,
            tool_calls=tool_calls,
            report=report,
            metrics=metrics,
            risk_policy=risk_policy,
        ),
    }


def create_eval_tool_registry(case: dict[str, Any] | None = None) -> ToolRegistry:
    """Create an eval registry with live Redis/MySQL golden adapters when configured."""
    case = case or {}
    registry = ToolRegistry()
    registry.register(QueryMetricsTool())
    registry.register(QueryLogsTool())
    registry.register(QueryRedisStatusTool())
    registry.register(QueryK8sStatusTool())
    registry.register(QueryMySQLStatusTool())
    registry.register(EvalRunbookTool(should_reject=bool(case.get("runbook_should_reject"))))
    registry.register(SearchHistoryTicketTool())
    registry.register(SuggestRemediationTool())
    apply_tool_fixtures(registry, case)
    return registry


def build_eval_incident(case: dict[str, Any]) -> Incident:
    """Build Incident from Alertmanager payload when a golden case provides one."""
    alertmanager_payload = case.get("alertmanager_payload")
    if isinstance(alertmanager_payload, dict):
        alerts = alertmanager_payload.get("alerts")
        if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
            event = _normalize_alertmanager_alert(alertmanager_payload, alerts[0])
            return _build_incident(event)
    return Incident(
        title=str(case.get("title") or case["id"]),
        **dict(case.get("incident") or {}),
    )


def golden_expectations(case: dict[str, Any]) -> dict[str, Any]:
    raw = case.get("golden")
    return dict(raw) if isinstance(raw, dict) else {}


def golden_evidence_mode(golden: dict[str, Any]) -> str:
    if golden.get("evidence_mode"):
        return str(golden["evidence_mode"])
    return "live_adapter" if golden.get("required_live_sources") else "deterministic_fixture"


def build_golden_chain_summary(
    *,
    case: dict[str, Any],
    state: dict[str, Any],
    plan_steps: list[PlanStep],
    evidence: list[Evidence],
    tool_calls: list[ToolCallRecord],
    report: Any,
    metrics: dict[str, bool],
    risk_policy: str,
) -> dict[str, Any]:
    """Return the full golden-path checklist used by reviewers and eval artifacts."""
    golden = golden_expectations(case)
    if not golden:
        return {}
    return {
        "alertmanager_payload": case.get("alertmanager_payload", {}),
        "incident_fields": {
            key: state["incident"].get(key)
            for key in ["incident_id", "service_name", "severity", "environment", "symptom"]
        },
        "planner_expected_steps": [
            {
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "purpose": step.purpose,
                "expected_evidence": step.expected_evidence,
                "risk_level": step.risk_level,
            }
            for step in plan_steps
        ],
        "actual_tool_order": [record.tool_name for record in tool_calls],
        "trace_completeness_basis": {
            "trace_id": report.trace_id,
            "tool_call_count": len(tool_calls),
            "evidence_count": len(evidence),
            "tool_call_statuses": [record.status for record in tool_calls],
            "source": "eval_tool_call_records_and_report_artifact",
        },
        "tool_sources": {
            record.tool_name: record.output.get("source", "")
            for record in tool_calls
            if isinstance(record.output, dict)
        },
        "evidence": [
            {
                "source_tool": item.source_tool,
                "data_source": item.data_source,
                "stance": item.stance,
                "fact": item.fact,
                "inference": item.inference,
                "uncertainty": item.uncertainty,
                "next_step": item.next_step,
                "confidence_reason": item.confidence_reason,
            }
            for item in evidence
        ],
        "root_cause": report.root_cause,
        "remediation_suggestion": report.remediation_suggestion,
        "approval": {
            "diagnosis_needs_approval": bool(
                golden.get(
                    "diagnosis_needs_approval",
                    case.get("expected_needs_approval", False),
                )
            ),
            "remediation_change_requires_approval": bool(
                golden.get("remediation_change_requires_approval", False)
            ),
            "expected_eval_needs_approval": bool(case.get("expected_needs_approval", False)),
        },
        "risk_policy": risk_policy,
        "report_must_contain": case.get("report_must_contain", []),
        "eval_case_id": case.get("id", ""),
        "live_source_requirements": golden.get("required_live_sources", {}),
        "evidence_mode": golden_evidence_mode(golden),
        "source_boundary": golden.get("source_boundary", ""),
        "acceptance_metrics": {
            key: metrics.get(key, False)
            for key in [
                "alertmanager_payload_hit",
                "incident_field_hit",
                "tool_sequence_hit",
                "evidence_fields_hit",
                "golden_signal_hit",
                "root_cause_hit",
                "report_structure_hit",
                "required_live_sources_hit",
                "runtime_vs_incident_boundary_hit",
                "approval_boundary_hit",
            ]
        },
    }


def expected_tool_order_hit(
    values: list[str],
    expected_prefix: list[str],
    *,
    strict: bool = False,
) -> bool:
    """Use strict prefix checks for golden chains and relative order elsewhere."""
    if strict:
        return starts_with_sequence(values, expected_prefix)
    return contains_relative_sequence(values, expected_prefix)


def alertmanager_payload_hit(case: dict[str, Any]) -> bool:
    golden = golden_expectations(case)
    if not golden:
        return True
    payload = case.get("alertmanager_payload")
    if not isinstance(payload, dict):
        return False
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts or not isinstance(alerts[0], dict):
        return False
    alert = alerts[0]
    labels = alert.get("labels")
    annotations = alert.get("annotations")
    required_label_keys = {"alertname", "service", "severity"}
    return bool(
        payload.get("receiver")
        and alert.get("status")
        and alert.get("startsAt")
        and alert.get("generatorURL")
        and isinstance(labels, dict)
        and required_label_keys.issubset(labels)
        and isinstance(annotations, dict)
        and (annotations.get("summary") or annotations.get("description"))
    )


def incident_field_hit(case: dict[str, Any], incident: dict[str, Any]) -> bool:
    golden = golden_expectations(case)
    if not golden:
        return True
    expected = dict(case.get("incident") or {})
    raw_alert = incident.get("raw_alert")
    return bool(
        incident.get("service_name") == expected.get("service_name")
        and incident.get("severity") == expected.get("severity")
        and incident.get("environment") == expected.get("environment")
        and incident.get("symptom")
        and isinstance(raw_alert, dict)
        and raw_alert.get("source") == "alertmanager"
        and raw_alert.get("alertname")
        and raw_alert.get("labels")
        and raw_alert.get("annotations")
    )


def evidence_fields_hit(evidence: list[Evidence], golden: dict[str, Any]) -> bool:
    if not golden:
        return True
    required_tools = set(golden.get("evidence_tools") or [])
    for item in evidence:
        if item.source_tool not in required_tools:
            continue
        if not all([item.fact, item.inference, item.uncertainty, item.next_step]):
            return False
    return bool(required_tools) and required_tools.issubset({item.source_tool for item in evidence})


def golden_signal_hit(tool_calls: list[ToolCallRecord], golden: dict[str, Any]) -> bool:
    if not golden:
        return True
    by_tool = {
        record.tool_name: record.output for record in tool_calls if record.status == "success"
    }
    for tool_name, requirements in dict(golden.get("required_output_signals") or {}).items():
        output = by_tool.get(tool_name)
        if not isinstance(output, dict):
            return False
        if not output_satisfies(output, dict(requirements)):
            return False
    return True


def required_live_sources_hit(
    tool_calls: list[ToolCallRecord],
    golden: dict[str, Any],
    *,
    case: dict[str, Any] | None = None,
) -> bool:
    """Verify golden Redis/MySQL adapter calls came from configured live data sources."""
    if not golden:
        return True
    requirements = dict(golden.get("required_live_sources") or {})
    if not requirements:
        return True
    by_tool = {
        record.tool_name: record.output for record in tool_calls if record.status == "success"
    }
    for tool_name, expected_source in requirements.items():
        output = by_tool.get(tool_name)
        if not isinstance(output, dict):
            return False
        if output.get("source") != expected_source:
            return False
    return True


def output_satisfies(output: dict[str, Any], requirements: dict[str, Any]) -> bool:
    for key, expected in requirements.items():
        value = nested_get(output, key)
        if isinstance(expected, dict):
            if "gte" in expected and not (
                isinstance(value, int | float) and value >= float(expected["gte"])
            ):
                return False
            if "lte" in expected and not (
                isinstance(value, int | float) and value <= float(expected["lte"])
            ):
                return False
            if (
                "contains" in expected
                and str(expected["contains"]).lower() not in str(value).lower()
            ):
                return False
            if "equals" in expected and value != expected["equals"]:
                return False
        elif value != expected:
            return False
    return True


def nested_get(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
            continue
        if isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
            continue
        return None
    return value


def report_structure_hit(report: Any, golden: dict[str, Any]) -> bool:
    if not golden:
        return True
    required_sections = [
        "### Tool Call Table",
        "### Evidence Quick View",
    ]
    markdown = str(report.markdown or "")
    return bool(
        report.root_cause
        and report.confidence_reason
        and report.next_steps
        and report.tool_calls
        and report.evidence
        and any(str(item.get("uncertainty") or "").strip() for item in report.evidence)
        and report.risk_summary is not None
        and report.approval_status
        and report.remediation_suggestion
        and all(section in markdown for section in required_sections)
    )


def evidence_sufficiency_hit(report: Any, golden: dict[str, Any]) -> bool:
    """Verify reports are gated by explicit evidence sufficiency, not only keywords."""
    if not golden:
        return True
    sufficiency = getattr(report, "evidence_sufficiency", {}) or {}
    evidence_profile = getattr(report, "evidence_profile", {}) or {}
    if not isinstance(sufficiency, dict):
        return False
    if str(getattr(report, "status", "")) == "completed":
        required_flags = [
            "complete",
            "has_primary_domain_evidence",
            "has_symptom_evidence",
            "has_reference_evidence",
        ]
        if not all(bool(sufficiency.get(flag)) for flag in required_flags):
            return False
    failed_tools = sufficiency.get("failed_tools", [])
    if failed_tools and "失败工具" not in str(getattr(report, "markdown", "")):
        return False
    markdown = str(getattr(report, "markdown", ""))
    required_text = ["证据充分性", "主故障域", "现象侧", "处置参考"]
    return all(item in markdown for item in required_text) and bool(
        evidence_profile.get("sufficiency_status") or sufficiency.get("status")
    )


def runtime_vs_incident_boundary_hit(
    report: Any,
    tool_calls: list[ToolCallRecord],
    golden: dict[str, Any],
) -> bool:
    if not golden:
        return True
    text_parts = [str(report.markdown or "")]
    for record in tool_calls:
        if isinstance(record.output, dict):
            text_parts.append(json.dumps(record.output, ensure_ascii=False, default=str))
    text = "\n".join(text_parts).lower()
    uses_replay_boundary = "incident_evidence" in text or "incident evidence" in text
    uses_runtime_boundary = "live_info" in text or "live_status" in text or "runtime" in text
    if "query_redis_status" in dict(golden.get("required_output_signals") or {}):
        return uses_replay_boundary and uses_runtime_boundary and "not actually saturated" in text
    if "query_mysql_status" in dict(golden.get("required_output_signals") or {}):
        return (
            uses_replay_boundary
            and uses_runtime_boundary
            and ("slow_queries counter" in text or "slow_queries" in text)
        )
    return True


def approval_boundary_hit(report: Any, golden: dict[str, Any], risk_policy: str) -> bool:
    if not golden:
        return True
    diagnosis_needs_approval = bool(golden.get("diagnosis_needs_approval", False))
    remediation_requires_approval = bool(golden.get("remediation_change_requires_approval", False))
    text = "\n".join(
        [
            str(report.markdown or ""),
            str(report.remediation_suggestion or ""),
            json.dumps(report.risk_summary or {}, ensure_ascii=False, default=str),
        ]
    ).lower()
    diagnosis_boundary_ok = (
        risk_policy == "approval_required"
        if diagnosis_needs_approval
        else risk_policy == "allow" and str(report.approval_status or "") == "not_required"
    )
    remediation_boundary_ok = (
        not remediation_requires_approval or "approval" in text or "审批" in text or "人工" in text
    )
    return diagnosis_boundary_ok and remediation_boundary_ok


def apply_tool_fixtures(registry: ToolRegistry, case: dict[str, Any]) -> None:
    """Apply per-case output overrides and failure injections."""
    outputs = default_tool_output_specs(case)
    outputs.update(tool_output_specs(case))
    for tool_name, output in outputs.items():
        tool = registry.get(tool_name)
        if tool is not None:
            registry.register(EvalStaticOutputTool(tool, output))

    for tool_name, failure in tool_failure_specs(case).items():
        tool = registry.get(tool_name)
        if tool is not None:
            registry.register(EvalFailureTool(tool, failure))


def tool_output_specs(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return static output fixtures keyed by tool name."""
    raw = case.get("tool_outputs") or {}
    return {str(name): dict(value) for name, value in raw.items()} if isinstance(raw, dict) else {}


def default_tool_output_specs(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return deterministic outputs, excluding live Redis/MySQL golden adapters when configured."""
    incident = dict(case.get("incident") or {})
    service_name = str(incident.get("service_name") or "unknown-service")
    text = " ".join(
        [
            str(case.get("id") or ""),
            str(case.get("title") or ""),
            str(case.get("input") or ""),
            str(incident.get("symptom") or ""),
        ]
    ).lower()
    outputs = {
        "query_metrics": _eval_metrics_output(service_name, text),
        "query_logs": _eval_logs_output(service_name, text),
        "query_redis_status": _eval_redis_output(service_name, text),
        "query_k8s_status": _eval_k8s_output(service_name, text),
        "query_mysql_status": _eval_mysql_output(service_name, text),
        "search_history_ticket": _eval_ticket_output(service_name, text),
    }
    for tool_name in live_golden_tools(case):
        outputs.pop(tool_name, None)
    return outputs


def live_golden_tools(case: dict[str, Any]) -> set[str]:
    """Let golden Redis/MySQL cases call configured live Docker adapters instead of mock fixtures."""
    if not golden_expectations(case):
        return set()
    case_id = str(case.get("id") or "")
    live_tools: set[str] = set()
    if "redis" in case_id and live_redis_configured():
        live_tools.update({"query_redis_status", *live_observability_tools()})
        if live_mysql_configured():
            live_tools.add("search_history_ticket")
    if "mysql" in case_id and live_mysql_configured():
        live_tools.update({"query_mysql_status", *live_observability_tools()})
        live_tools.add("search_history_ticket")
    return live_tools


def live_redis_configured() -> bool:
    return bool(
        os.environ.get("REDIS_URL")
        or os.environ.get("REDIS_INSTANCES")
        or os.environ.get("REDIS_HOST")
    )


def live_mysql_configured() -> bool:
    return bool(
        os.environ.get("MYSQL_DSN")
        or os.environ.get("MYSQL_URL")
        or os.environ.get("MYSQL_INSTANCES")
        or os.environ.get("MYSQL_HOST")
    )


def live_observability_tools() -> set[str]:
    tools: set[str] = set()
    if os.environ.get("PROMETHEUS_BASE_URL"):
        tools.add("query_metrics")
    if os.environ.get("LOKI_BASE_URL") or os.environ.get("LOG_GATEWAY_URL"):
        tools.add("query_logs")
    return tools


def _eval_metrics_output(service_name: str, text: str) -> dict[str, Any]:
    cpu_high = "cpu" in text
    memory_high = "memory" in text or "oom" in text or "内存" in text
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "qps": {"current": 1280, "baseline": 900, "trend": "up"},
        "p95_latency_ms": {"current": 3250, "threshold": 1000, "status": "high"},
        "error_rate": {"current": 0.082, "threshold": 0.01, "status": "high"},
        "cpu": {
            "statistics": {"avg": 86.2 if cpu_high else 72.5, "max": 96.0 if cpu_high else 91.2},
            "alert_info": {"triggered": True, "message": "CPU 使用率超过阈值"},
        },
        "memory": {
            "statistics": {
                "avg": 91.5 if memory_high else 68.1,
                "max": 98.0 if memory_high else 79.4,
            },
            "alert_info": {
                "triggered": memory_high,
                "message": "Memory 使用率超过阈值" if memory_high else "内存未超过关键阈值",
            },
        },
        "summary": f"{service_name} P95=3250ms, 5xx=8.20%, CPU 指标异常",
    }


def _eval_logs_output(service_name: str, text: str) -> dict[str, Any]:
    if "disk" in text or "no space" in text or "磁盘" in text:
        message = f"{service_name} write failed: no space left on device"
    elif "mysql" in text or "sql" in text or "慢查询" in text:
        message = f"{service_name} MySQL slow query timeout and connection pool waiting"
    elif "pod" in text or "crashloop" in text or "oom" in text:
        message = f"{service_name} Pod CrashLoopBackOff OOMKilled restart count increasing"
    elif "unknown-service" in text or "无法归类" in text:
        message = f"{service_name} P95 latency high with unclassified business anomaly"
    else:
        message = f"{service_name} Redis connection timeout and request failed with 5xx"
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "logs": {"total": 2, "logs": [{"level": "ERROR", "message": message}]},
        "summary": f"eval fixture 日志发现异常: {message}",
    }


def _eval_redis_output(service_name: str, text: str) -> dict[str, Any]:
    normal = "正常" in text or "look normal" in text or "status conflict" in text
    connected_clients = 1200 if normal else 9940
    maxclients = 10000
    usage = connected_clients / maxclients
    live_connected_clients = 1 if not normal else connected_clients
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "connected_clients": connected_clients,
        "maxclients": maxclients,
        "client_usage_ratio": round(usage, 4),
        "blocked_clients": 0 if normal else 37,
        "slowlog": [] if normal else [{"command": "HGETALL order:cache:*", "duration_ms": 128}],
        "alert_info": {
            "triggered": not normal,
            "message": "Redis 连接数正常" if normal else "Redis connected_clients 接近 maxclients",
        },
        "incident_evidence": {
            "_key": "eval_fixture:redis-maxclients",
            "connected_clients": connected_clients,
            "maxclients": maxclients,
        },
        "live_info": {
            "connected_clients": live_connected_clients,
            "maxclients": maxclients,
            "scope": "current eval runtime snapshot",
        },
        "fact": (
            f"Redis evidence key shows connected_clients close to maxclients: "
            f"{connected_clients}/{maxclients}, blocked_clients={0 if normal else 37}."
        ),
        "inference": (
            "Application Redis connection acquisition likely waited or timed out, "
            "which explains the order-service 5xx and timeout spike."
        ),
        "uncertainty": (
            f"Current Redis runtime is not actually saturated "
            f"(live_info connected_clients={live_connected_clients}/{maxclients}); "
            "the saturation evidence comes from the replay incident window stored in Redis."
        ),
        "summary": f"connected_clients={connected_clients}/{maxclients}, usage={usage:.2%}",
    }


def _eval_k8s_output(service_name: str, text: str) -> dict[str, Any]:
    abnormal = any(token in text for token in ["pod", "crashloop", "oom", "disk", "no space"])
    status = "CrashLoopBackOff" if abnormal else "Running"
    restarts = 12 if abnormal else 1
    event_reason = "OOMKilled" if "oom" in text else ("FailedMount" if "disk" in text else status)
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "pods": [
            {
                "name": f"{service_name}-7f8d9c-abc12",
                "ready": not abnormal,
                "restarts": restarts,
                "status": status,
                "last_state": event_reason if abnormal else "",
            }
        ],
        "events": (
            [{"reason": event_reason, "message": f"{event_reason} observed"}] if abnormal else []
        ),
        "summary": (
            f"Kubernetes Pod {status}, restarts={restarts}, event={event_reason}"
            if abnormal
            else "Pod Running，未发现异常"
        ),
    }


def _eval_mysql_output(service_name: str, text: str) -> dict[str, Any]:
    active = "mysql" in text or "sql" in text or "slow query" in text or "慢查询" in text
    slow_query_count = 18 if active else 0
    runtime_slow_queries = 0 if active else 0
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "slow_queries": (
            [
                {
                    "sql_digest": "select * from orders where user_id=?",
                    "avg_ms": 920,
                    "count": slow_query_count,
                }
            ]
            if active
            else []
        ),
        "connections": {
            "active": 188 if active else 84,
            "max": 200,
            "pool_waiting": 6 if active else 0,
        },
        "lock_waits": 3 if active else 0,
        "incident_evidence": {
            "case_id": "INC-MYSQL-001",
            "observed_value": "slow_queries=18,pool_waiting=6,active_connections=188/200",
            "evidence_source": "eval_fixture",
        },
        "live_status": {
            "Slow_queries": runtime_slow_queries,
            "Threads_connected": 2 if active else 1,
            "scope": "current MySQL runtime counters",
        },
        "evidence_chain": [
            {
                "stage": "slow_sql",
                "fact": "slow_query_count=18, avg_ms=920.",
                "inference": "The SQL path is slower than the service latency budget.",
                "uncertainty": "Slow SQL count comes from incident evidence/payment_events.",
            },
            {
                "stage": "connection_pool_wait",
                "fact": "active_connections=188/200, pool_waiting=6.",
                "inference": "Slow SQL occupied connections and pushed callers into pool wait.",
                "uncertainty": "Pool waiting is application-side incident evidence.",
            },
            {
                "stage": "user_impact",
                "fact": "payment-service p95 latency and MySQL slow-query symptoms overlap.",
                "inference": "Users experienced elevated latency from DB waits.",
                "uncertainty": "User impact should be cross-checked with metrics/logs.",
            },
        ],
        "fact": (
            "MySQL incident evidence shows slow_query_count=18, "
            "active_connections=188/200, pool_waiting=6."
            if active
            else "MySQL runtime counters do not show key slow-query evidence."
        ),
        "inference": (
            "Slow SQL held database connections long enough to drive application "
            "connection-pool waiting, causing payment-service latency and user impact."
        ),
        "uncertainty": (
            f"Current MySQL runtime Slow_queries counter is {runtime_slow_queries}; the main "
            "diagnostic evidence comes from live incident evidence tables and the "
            "payment_events slow-query record."
        ),
        "summary": (
            "MySQL 慢查询累计增加，连接池等待" if active else "MySQL 连接和慢查询未见关键异常"
        ),
    }


def _eval_ticket_output(service_name: str, text: str) -> dict[str, Any]:
    if "mysql" in text or "sql" in text:
        root_cause = "MySQL 慢查询和连接池等待导致接口延迟"
    elif "pod" in text or "crashloop" in text:
        root_cause = "Kubernetes Pod CrashLoopBackOff 导致容量下降"
    else:
        root_cause = "Redis 连接数达到 maxclients，应用连接池未及时释放连接"
    return {
        "service_name": service_name,
        "source": "eval_fixture",
        "tickets": [{"ticket_id": "INC-EVAL-001", "root_cause": root_cause}],
        "summary": f"找到 1 条相似故障: {root_cause}",
    }


def tool_failure_specs(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return failure fixtures keyed by tool name."""
    raw = case.get("tool_failures") or {}
    if isinstance(raw, dict):
        return {str(name): dict(value) for name, value in raw.items()}
    if not isinstance(raw, list):
        return {}

    specs: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("tool_name"):
            continue
        specs[str(item["tool_name"])] = dict(item)
    return specs


async def execute_safe_eval_steps(
    steps: list[PlanStep],
    registry: ToolRegistry,
    *,
    trace_id: str,
    incident_id: str,
) -> tuple[list[Evidence], list[ToolCallRecord]]:
    """Execute only steps allowed by risk policy and convert output into evidence."""
    evidence: list[Evidence] = []
    tool_calls: list[ToolCallRecord] = []

    for step in steps:
        decision = assess_plan_step(step, tool_registry=registry)
        if decision.policy != "allow":
            continue
        if registry.get(step.tool_name) is None:
            continue
        result = await registry.arun(step.tool_name, step.input_args)
        evidence.append(tool_result_to_evidence(result, step))
        tool_calls.append(tool_result_to_call_record(result, step, trace_id, incident_id))

    return evidence, tool_calls


def tool_result_to_evidence(result: ToolExecutionResult, step: PlanStep) -> Evidence:
    """Convert tool result to Evidence using the same schema as Executor."""
    return executor_tool_result_to_evidence(result, step)


def tool_result_to_call_record(
    result: ToolExecutionResult,
    step: PlanStep,
    trace_id: str,
    incident_id: str,
) -> ToolCallRecord:
    """Convert tool result to ToolCallRecord using the same schema as Executor."""
    return executor_tool_result_to_call_record(
        result,
        step,
        {
            "trace_id": trace_id,
            "incident": {"incident_id": incident_id},
        },
    )


def summarize_tool_result(result: ToolExecutionResult) -> str:
    """Build compact evidence text for eval reports."""
    if result.status == "failed":
        return f"工具 {result.tool_name} 调用失败: {result.error_message or '未知错误'}"
    if isinstance(result.output, dict) and result.output.get("summary"):
        return str(result.output["summary"])
    return f"工具 {result.tool_name} 调用成功"


def build_summary(
    results: list[dict[str, Any]], *, rag_payload: dict[str, Any] | None
) -> dict[str, Any]:
    """Build aggregate metrics and category summaries."""
    rag_summary = (rag_payload or {}).get("summary", {})
    aiops_case_count = len(results)
    aiops_passed_count = sum(1 for result in results if result["passed"])
    rag_case_count = int(rag_summary.get("case_count", 0)) if rag_payload else 0
    rag_passed_count = int(rag_summary.get("passed_count", 0)) if rag_payload else 0
    overall_case_count = aiops_case_count + rag_case_count
    overall_passed_count = aiops_passed_count + rag_passed_count
    metric_summary = {
        metric: {
            "passed": sum(1 for result in results if result["metrics"].get(metric)),
            "total": aiops_case_count,
        }
        for metric in METRIC_NAMES
    }
    failed_cases = build_aiops_failed_cases(results) + build_rag_failed_cases(rag_payload)
    summary = {
        "case_count": aiops_case_count,
        "passed_count": aiops_passed_count,
        "overall_case_count": overall_case_count,
        "overall_passed_count": overall_passed_count,
        "overall_pass_rate": ratio(overall_passed_count, overall_case_count),
        "all_passed": overall_passed_count == overall_case_count,
        "metrics": metric_summary,
        "failed_cases": failed_cases,
        "p95_latency_ms": percentile([result.get("latency_ms", 0.0) for result in results], 0.95),
        "avg_latency_ms": average([result.get("latency_ms", 0.0) for result in results]),
        "rag_case_count": rag_case_count,
        "rag_passed_count": rag_passed_count,
    }
    summary["pass_rate"] = ratio(summary["passed_count"], summary["case_count"])
    summary["categories"] = build_category_metrics(results, metric_summary, rag_payload=rag_payload)
    summary["resume_metrics"] = build_resume_metrics(summary, rag_payload=rag_payload)
    return summary


def build_aiops_failed_cases(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return failed AIOps cases with actionable metric reasons."""
    return [
        {
            "suite": "aiops",
            "id": result["id"],
            "failed_metrics": result["failed_metrics"],
            "failure_reasons": result["failure_reasons"],
        }
        for result in results
        if not result["passed"]
    ]


def build_rag_failed_cases(rag_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return failed RAG cases with retrieval context."""
    if not rag_payload:
        return []
    failed_cases = rag_payload.get("summary", {}).get("failed_cases", [])
    return [
        {
            "suite": "rag",
            "id": item.get("id", ""),
            "failed_metrics": item.get("failed_metrics", []),
            "failure_reasons": item.get("failure_reasons", {}),
            "retrieved_sources": item.get("retrieved_sources", []),
            "expected_sources": item.get("expected_sources", []),
        }
        for item in failed_cases
    ]


def build_category_metrics(
    results: list[dict[str, Any]],
    metric_summary: dict[str, dict[str, int]],
    *,
    rag_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Group low-level metrics into resume-friendly quality dimensions."""
    forbidden_results = [
        result for result in results if result.get("expected_risk_policy") == "forbidden"
    ]
    approval_results = [result for result in results if result.get("expected_needs_approval")]
    failure_results = [result for result in results if result.get("expected_failed_tools")]
    runbook_reject_results = [result for result in results if result.get("runbook_should_reject")]
    rag_summary = (rag_payload or {}).get("summary", {})

    return {
        "diagnosis": {
            "root_cause_hit_rate": metric_rate(metric_summary, "root_cause_hit"),
            "evidence_count_hit_rate": metric_rate(metric_summary, "evidence_count_hit"),
            "confidence_hit_rate": metric_rate(metric_summary, "confidence_hit"),
            "average_evidence_count": average(
                [result.get("evidence_count", 0) for result in results]
            ),
            "average_confidence": average([result.get("confidence", 0.0) for result in results]),
        },
        "tool": {
            "tool_hit_rate": metric_rate(metric_summary, "tool_hit"),
            "tool_order_hit_rate": metric_rate(metric_summary, "tool_sequence_hit"),
            "executed_tool_hit_rate": metric_rate(metric_summary, "executed_tool_hit"),
        },
        "risk": {
            "risk_policy_hit_rate": metric_rate(metric_summary, "risk_policy_hit"),
            "forbidden_action_block_rate": ratio(
                sum(1 for result in forbidden_results if result.get("risk_policy") == "forbidden"),
                len(forbidden_results),
            ),
            "forbidden_case_count": len(forbidden_results),
            "approval_recall": ratio(
                sum(1 for result in approval_results if result["metrics"].get("approval_hit")),
                len(approval_results),
            ),
            "approval_case_count": len(approval_results),
        },
        "rag": {
            "case_count": rag_summary.get("case_count", 0),
            "recall_at_1": rag_summary.get("recall_at_1", 0.0),
            "recall_at_k": rag_summary.get("recall_at_k", 0.0),
            "top_k": rag_summary.get("top_k", 0),
            "mrr": rag_summary.get("mrr", 0.0),
            "citation_coverage_rate": rag_summary.get("citation_coverage_rate", 0.0),
            "no_answer_rejection_rate": rag_summary.get("no_answer_rejection_rate", 0.0),
            "confusion_case_pass_rate": rag_summary.get("confusion_case_pass_rate", 0.0),
            "confusion_case_count": rag_summary.get("confusion_case_count", 0),
            "runbook_no_answer_rejection_hit_rate": ratio(
                sum(1 for result in runbook_reject_results if result.get("runbook_rejected")),
                len(runbook_reject_results),
            ),
            "runbook_rejection_case_count": len(runbook_reject_results),
        },
        "stability": {
            "tool_failure_case_count": len(failure_results),
            "tool_failure_graceful_degradation_rate": ratio(
                sum(
                    1
                    for result in failure_results
                    if result["metrics"].get("tool_failure_graceful_degradation")
                ),
                len(failure_results),
            ),
            "failed_tool_observed_rate": ratio(
                sum(
                    1
                    for result in failure_results
                    if contains_all(
                        result.get("failed_tools", []), result.get("expected_failed_tools", [])
                    )
                ),
                len(failure_results),
            ),
        },
        "diagnostic_chain": {
            "scope_note": "诊断链路离线评测指标，用于验证工具选择、证据、假设、风控、报告和 Trace 闭环，不代表线上根因准确率。",
            "tool_selection_recall": metric_rate(metric_summary, "tool_selection_recall"),
            "unnecessary_tool_rate": metric_rate(metric_summary, "unnecessary_tool_rate"),
            "root_cause_hit": metric_rate(metric_summary, "hypothesis_ranking_hit"),
            "evidence_support_rate": metric_rate(metric_summary, "evidence_support_rate"),
            "approval_recall": metric_rate(metric_summary, "approval_recall"),
            "forbidden_precision": metric_rate(metric_summary, "forbidden_precision"),
            "degradation_success": metric_rate(metric_summary, "degradation_success"),
            "trace_completeness": metric_rate(metric_summary, "trace_completeness"),
            "evidence_sufficiency": metric_rate(metric_summary, "evidence_sufficiency_hit"),
            "runtime_vs_incident_boundary": metric_rate(
                metric_summary,
                "runtime_vs_incident_boundary_hit",
            ),
            "approval_boundary": metric_rate(metric_summary, "approval_boundary_hit"),
        },
    }


def build_resume_metrics(
    summary: dict[str, Any],
    *,
    rag_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the single source of truth for resume numbers."""
    categories = summary["categories"]
    rag_summary = (rag_payload or {}).get("summary", {})
    return {
        "aiops_case_count": summary["case_count"],
        "aiops_pass_rate": summary["pass_rate"],
        "p95_latency_ms": summary["p95_latency_ms"],
        "root_cause_hit_rate": categories["diagnosis"]["root_cause_hit_rate"],
        "tool_hit_rate": categories["tool"]["tool_hit_rate"],
        "tool_order_hit_rate": categories["tool"]["tool_order_hit_rate"],
        "executed_tool_hit_rate": categories["tool"]["executed_tool_hit_rate"],
        "approval_recall": categories["risk"]["approval_recall"],
        "forbidden_action_block_rate": categories["risk"]["forbidden_action_block_rate"],
        "report_generation_rate": metric_rate(summary["metrics"], "report_generated"),
        "tool_failure_graceful_degradation_rate": categories["stability"][
            "tool_failure_graceful_degradation_rate"
        ],
        "diagnostic_tool_selection_recall": categories["diagnostic_chain"]["tool_selection_recall"],
        "diagnostic_unnecessary_tool_rate": categories["diagnostic_chain"]["unnecessary_tool_rate"],
        "diagnostic_root_cause_hit": categories["diagnostic_chain"]["root_cause_hit"],
        "diagnostic_evidence_support_rate": categories["diagnostic_chain"]["evidence_support_rate"],
        "diagnostic_trace_completeness": categories["diagnostic_chain"]["trace_completeness"],
        "diagnostic_evidence_sufficiency": categories["diagnostic_chain"]["evidence_sufficiency"],
        "diagnostic_runtime_vs_incident_boundary": categories["diagnostic_chain"][
            "runtime_vs_incident_boundary"
        ],
        "diagnostic_approval_boundary": categories["diagnostic_chain"]["approval_boundary"],
        "rag_case_count": rag_summary.get("case_count", 0),
        "rag_recall_at_k": categories["rag"]["recall_at_k"],
        "rag_mrr": categories["rag"]["mrr"],
        "rag_citation_coverage_rate": categories["rag"]["citation_coverage_rate"],
        "rag_no_answer_rejection_rate": categories["rag"]["no_answer_rejection_rate"],
        "rag_confusion_case_pass_rate": categories["rag"]["confusion_case_pass_rate"],
    }


def build_exception_result(case: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """Create a failed result for a case that raised unexpectedly."""
    metrics = dict.fromkeys(METRIC_NAMES, False)
    failed_metrics = failed_metric_names(metrics)
    return {
        "id": str(case.get("id") or "unknown"),
        "passed": False,
        "metrics": metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": {
            metric: f"{METRIC_FAILURE_REASONS.get(metric, metric)} 异常: {exc}"
            for metric in failed_metrics
        },
        "planned_tools": [],
        "executed_tools": [],
        "failed_tools": [],
        "expected_failed_tools": case.get("expected_failed_tools", []),
        "forbidden_tools": case.get("forbidden_tools", []),
        "risk_policy": "error",
        "expected_risk_policy": case.get("expected_risk_policy", "allow"),
        "expected_needs_approval": bool(case.get("expected_needs_approval", False)),
        "report_status": "error",
        "hypotheses": [],
        "evidence_count": 0,
        "report_id": "",
        "confidence": 0.0,
        "runbook_rejected": False,
        "runbook_should_reject": bool(case.get("runbook_should_reject", False)),
        "tool_latency_ms": [],
    }


def strongest_policy(decisions: list[Any]) -> str:
    """Return the strongest risk policy in a list of decisions."""
    policies = [decision.policy for decision in decisions]
    if "forbidden" in policies:
        return "forbidden"
    if "approval_required" in policies:
        return "approval_required"
    return "allow"


def contains_all(values: list[str], expected: list[str]) -> bool:
    """Return True when all expected values are present."""
    return all(item in values for item in expected)


def starts_with_sequence(values: list[str], expected_prefix: list[str]) -> bool:
    """Return True when values start with the expected prefix."""
    if not expected_prefix:
        return True
    return values[: len(expected_prefix)] == expected_prefix


def contains_relative_sequence(values: list[str], expected_sequence: list[str]) -> bool:
    """Return True when expected tools appear in order, allowing context tools between them."""
    if not expected_sequence:
        return True
    cursor = 0
    for value in values:
        if value == expected_sequence[cursor]:
            cursor += 1
            if cursor == len(expected_sequence):
                return True
    return False


def diagnostic_unnecessary_tool_rate(planned_tools: list[str], expected_tools: list[str]) -> float:
    """Score unrelated diagnostic tools while allowing standard context/trace enrichment."""
    allowed_enrichment = {
        "query_alerts",
        "query_service_context",
        "query_deploy_history",
    }
    expected = set(expected_tools) | allowed_enrichment
    unexpected = [tool for tool in planned_tools if tool not in expected]
    return ratio(len(unexpected), len(planned_tools))


def has_no_overlap(values: list[str], forbidden: list[str]) -> bool:
    """Return True when none of the forbidden values are present."""
    value_set = set(values)
    return all(item not in value_set for item in forbidden)


def text_has_all(text: str, expected_keywords: list[str]) -> bool:
    """Return True when every keyword appears in text."""
    lowered = text.lower()
    return all(str(keyword).lower() in lowered for keyword in expected_keywords)


def runbook_rejected(tool_calls: list[ToolCallRecord]) -> bool:
    """Return True when the eval Runbook tool rejected an unsupported query."""
    for record in tool_calls:
        if record.tool_name != "search_runbook" or not isinstance(record.output, dict):
            continue
        if record.output.get("no_answer_rejected"):
            return True
    return False


def failed_metric_names(metrics: dict[str, bool]) -> list[str]:
    """Return failed metric names in stable display order."""
    return [name for name in METRIC_NAMES if not metrics.get(name)]


def failure_reasons(failed_metrics: list[str]) -> dict[str, str]:
    """Map failed metric names to human-readable reasons."""
    return {metric: METRIC_FAILURE_REASONS.get(metric, metric) for metric in failed_metrics}


def metric_rate(metric_summary: dict[str, dict[str, int]], metric: str) -> float:
    """Return pass rate for one low-level metric."""
    item = metric_summary.get(metric, {})
    return ratio(int(item.get("passed", 0)), int(item.get("total", 0)))


def ratio(numerator: int | float, denominator: int | float) -> float:
    """Return a rounded ratio while handling empty denominators."""
    return round(float(numerator) / max(float(denominator), 1.0), 4)


def average(values: list[int | float]) -> float:
    """Return a rounded average."""
    if not values:
        return 0.0
    return round(sum(float(value) for value in values) / len(values), 4)


def percentile(values: list[int | float], quantile: float) -> float:
    """Return nearest-rank percentile for small deterministic eval runs."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 2)


def render_summary(payload: dict[str, Any]) -> str:
    """Render a compact console summary."""
    summary = payload["summary"]
    categories = summary["categories"]
    lines = [
        (
            f"Full eval: {summary['overall_passed_count']}/"
            f"{summary['overall_case_count']} cases passed "
            f"({summary['overall_pass_rate']:.0%}); "
            f"AIOps={summary['passed_count']}/{summary['case_count']}, "
            f"RAG={summary['rag_passed_count']}/{summary['rag_case_count']}; "
            f"p95_latency={summary['p95_latency_ms']:.2f}ms"
        ),
        (
            f"AIOps eval: {summary['passed_count']}/"
            f"{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%}); "
            f"p95_latency={summary['p95_latency_ms']:.2f}ms"
        ),
        (
            "Metrics: "
            f"root={categories['diagnosis']['root_cause_hit_rate']:.0%}, "
            f"tool={categories['tool']['tool_hit_rate']:.0%}, "
            f"approval={categories['risk']['approval_recall']:.0%}, "
            f"forbidden={categories['risk']['forbidden_action_block_rate']:.0%}, "
            f"stability={categories['stability']['tool_failure_graceful_degradation_rate']:.0%}, "
            f"RAG recall@{categories['rag']['top_k']}={categories['rag']['recall_at_k']:.0%}, "
            f"RAG cite={categories['rag']['citation_coverage_rate']:.0%}, "
            f"RAG confusion={categories['rag']['confusion_case_pass_rate']:.0%}, "
            f"RAG reject={categories['rag']['no_answer_rejection_rate']:.0%}"
        ),
    ]
    for result in payload["cases"]:
        status = "PASS" if result["passed"] else "FAIL"
        suffix = "" if result["passed"] else f" failed={','.join(result['failed_metrics'])}"
        lines.append(
            f"- {status} {result['id']} policy={result['risk_policy']} "
            f"latency={result.get('latency_ms', 0.0):.2f}ms{suffix}"
        )
    for result in payload.get("rag", {}).get("cases", []):
        status = "PASS" if result["passed"] else "FAIL"
        suffix = "" if result["passed"] else f" failed={','.join(result['failed_metrics'])}"
        lines.append(
            f"- RAG {status} {result['id']} top_score={result.get('top_score', 0.0):.2f}{suffix}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    """Render a reproducible Markdown summary for resume metrics."""
    run = payload["run"]
    summary = payload["summary"]
    categories = summary["categories"]
    resume = summary["resume_metrics"]
    rag = payload.get("rag", {}).get("summary", {})
    failed_cases = summary["failed_cases"]

    lines = [
        "# AutoOnCall 离线评测摘要",
        "",
        "## 运行记录",
        f"- 生成时间：{run.get('ended_at', '')}",
        f"- AIOps case 文件：`{run.get('cases_path', '')}`",
        f"- RAG case 文件：`{run.get('rag_cases_path', '') or '未运行'}`",
        f"- 报告数据库：`{run.get('report_path', '')}`",
        f"- 总耗时：{run.get('duration_ms', 0.0):.2f} ms",
        f"- 评测边界：{run.get('evaluation_scope', '')}",
        f"- p95 case latency：{summary['p95_latency_ms']:.2f} ms",
        f"- 完整评测通过率：{summary['overall_passed_count']}/{summary['overall_case_count']} ({summary['overall_pass_rate']:.0%})",
        "",
        "## 简历可摘取指标",
        f"- AIOps 离线 case：{resume['aiops_case_count']} 个，通过率 {resume['aiops_pass_rate']:.0%}",
        f"- 工具命中率：{resume['tool_hit_rate']:.0%}，工具顺序命中率：{resume['tool_order_hit_rate']:.0%}，实际执行工具命中率：{resume['executed_tool_hit_rate']:.0%}",
        f"- 根因命中率：{resume['root_cause_hit_rate']:.0%}，报告生成率：{resume['report_generation_rate']:.0%}",
        f"- 审批召回率：{resume['approval_recall']:.0%}，禁止动作拦截率：{resume['forbidden_action_block_rate']:.0%}",
        f"- 工具失败降级报告率：{resume['tool_failure_graceful_degradation_rate']:.0%}",
        f"- 诊断链路：工具选择召回 {resume['diagnostic_tool_selection_recall']:.0%}，假设根因命中 {resume['diagnostic_root_cause_hit']:.0%}，证据充分性 {resume['diagnostic_evidence_sufficiency']:.0%}，Trace 完整性 {resume['diagnostic_trace_completeness']:.0%}",
        f"- RAG case：{resume['rag_case_count']} 个，recall@{categories['rag']['top_k']} {resume['rag_recall_at_k']:.0%}，MRR {resume['rag_mrr']:.2f}，引用覆盖率 {resume['rag_citation_coverage_rate']:.0%}，混淆 case 通过率 {resume['rag_confusion_case_pass_rate']:.0%}，无答案拒答率 {resume['rag_no_answer_rejection_rate']:.0%}",
        "",
        "> 诊断链路指标用于验证离线 case 中的工具选择、证据、假设排序、风控、报告和 Trace 闭环，不代表线上根因准确率。",
        "",
        "## 分类指标",
        "| 分类 | 指标 | 数值 |",
        "| --- | --- | ---: |",
        f"| 诊断 | root cause hit | {categories['diagnosis']['root_cause_hit_rate']:.0%} |",
        f"| 诊断 | evidence count hit | {categories['diagnosis']['evidence_count_hit_rate']:.0%} |",
        f"| 诊断 | confidence hit | {categories['diagnosis']['confidence_hit_rate']:.0%} |",
        f"| 工具 | tool hit | {categories['tool']['tool_hit_rate']:.0%} |",
        f"| 工具 | tool order hit | {categories['tool']['tool_order_hit_rate']:.0%} |",
        f"| 工具 | executed tool hit | {categories['tool']['executed_tool_hit_rate']:.0%} |",
        f"| 风控 | forbidden action block rate | {categories['risk']['forbidden_action_block_rate']:.0%} |",
        f"| 风控 | approval recall | {categories['risk']['approval_recall']:.0%} |",
        f"| RAG | recall@{categories['rag']['top_k']} | {categories['rag']['recall_at_k']:.0%} |",
        f"| RAG | MRR | {categories['rag']['mrr']:.2f} |",
        f"| RAG | no-answer rejection | {categories['rag']['no_answer_rejection_rate']:.0%} |",
        f"| 稳定性 | tool failure graceful degradation | {categories['stability']['tool_failure_graceful_degradation_rate']:.0%} |",
        f"| 诊断链路 | tool selection recall | {categories['diagnostic_chain']['tool_selection_recall']:.0%} |",
        f"| 诊断链路 | unnecessary tool rate pass | {categories['diagnostic_chain']['unnecessary_tool_rate']:.0%} |",
        f"| 诊断链路 | root cause hit | {categories['diagnostic_chain']['root_cause_hit']:.0%} |",
        f"| 诊断链路 | evidence support rate pass | {categories['diagnostic_chain']['evidence_support_rate']:.0%} |",
        f"| 诊断链路 | approval recall | {categories['diagnostic_chain']['approval_recall']:.0%} |",
        f"| 诊断链路 | forbidden precision | {categories['diagnostic_chain']['forbidden_precision']:.0%} |",
        f"| 诊断链路 | degradation success | {categories['diagnostic_chain']['degradation_success']:.0%} |",
        f"| 诊断链路 | trace completeness | {categories['diagnostic_chain']['trace_completeness']:.0%} |",
        f"| 诊断链路 | evidence sufficiency gate | {categories['diagnostic_chain']['evidence_sufficiency']:.0%} |",
        "",
        "## 失败定位",
    ]
    if failed_cases:
        for item in failed_cases:
            detail = (
                f"；期望来源：{', '.join(item.get('expected_sources', [])) or '-'}；"
                f"实际来源：{', '.join(item.get('retrieved_sources', [])) or '-'}"
                if item.get("suite") == "rag"
                else ""
            )
            lines.append(
                f"- [{item.get('suite', 'aiops')}] {item['id']}："
                f"{', '.join(item['failed_metrics'])}；"
                f"{'; '.join(item['failure_reasons'].values())}"
                f"{detail}"
            )
    else:
        lines.append("- 无失败 case。")

    lines.extend(
        [
            "",
            "## AIOps Case 明细",
            "| Case | 结果 | 风险策略 | 证据数 | 置信度 | 耗时(ms) | 失败指标 |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for result in payload["cases"]:
        failed = ", ".join(result["failed_metrics"]) if result["failed_metrics"] else "-"
        lines.append(
            "| "
            f"{result['id']} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | "
            f"{result['risk_policy']} | "
            f"{result['evidence_count']} | "
            f"{result['confidence']:.2f} | "
            f"{result.get('latency_ms', 0.0):.2f} | "
            f"{failed} |"
        )

    if rag:
        lines.extend(
            [
                "",
                "## RAG 指标来源",
                f"- RAG case 数：{rag.get('case_count', 0)}",
                f"- recall@1：{rag.get('recall_at_1', 0.0):.0%}",
                f"- recall@{rag.get('top_k', 0)}：{rag.get('recall_at_k', 0.0):.0%}",
                f"- MRR：{rag.get('mrr', 0.0):.2f}",
                f"- citation coverage：{rag.get('citation_coverage_rate', 0.0):.0%}",
                f"- confusion case pass：{rag.get('confusion_case_pass_rate', 0.0):.0%}",
                f"- no-answer rejection：{rag.get('no_answer_rejection_rate', 0.0):.0%}",
            ]
        )
        failed_rag_cases = rag.get("failed_cases", [])
        if failed_rag_cases:
            lines.extend(["", "## RAG 失败明细"])
            for item in failed_rag_cases:
                lines.append(
                    f"- {item['id']}：{', '.join(item['failed_metrics'])}；"
                    f"期望来源：{', '.join(item.get('expected_sources', [])) or '-'}；"
                    f"实际来源：{', '.join(item.get('retrieved_sources', [])) or '-'}"
                )

    return "\n".join(lines) + "\n"


def write_eval_artifacts(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
) -> dict[str, str]:
    """Write JSON and Markdown eval summaries."""
    written: dict[str, str] = {}
    if summary_json_path:
        written["summary_json"] = str(Path(summary_json_path))
    if summary_md_path:
        written["summary_md"] = str(Path(summary_md_path))

    payload["run"]["artifacts"] = written

    if summary_json_path:
        path = Path(summary_json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary_md_path:
        path = Path(summary_md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown_summary(payload), encoding="utf-8")
    return written


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Run offline AIOps evaluation cases.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
    parser.add_argument("--rag-cases", default=str(DEFAULT_RAG_CASES_PATH))
    parser.add_argument("--rag-docs-dir", default=str(DEFAULT_RAG_DOCS_DIR))
    parser.add_argument(
        "--env-file",
        default="",
        help="Optional KEY=VALUE env file loaded before creating live adapters.",
    )
    parser.add_argument("--skip-rag", action="store_true", help="Skip embedded RAG eval metrics")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text summary")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    load_env_file(args.env_file)
    payload = asyncio.run(
        evaluate_cases(
            args.cases,
            report_path=args.report_path,
            include_rag=not args.skip_rag,
            rag_cases_path=args.rag_cases,
            rag_docs_dir=args.rag_docs_dir,
        )
    )
    payload["run"]["command"] = " ".join(sys.argv)
    written = write_eval_artifacts(
        payload,
        summary_json_path=args.summary_json,
        summary_md_path=args.summary_md,
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_summary(payload))
        if written:
            print("Artifacts: " + ", ".join(f"{key}={value}" for key, value in written.items()))
    return 0 if payload["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
