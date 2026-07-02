"""Read models for offline evaluation dashboards."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_eval_summary_payload(
    raw_payload: dict[str, Any],
    *,
    summary_path: Path,
) -> dict[str, Any]:
    """Build the frontend/API payload for an available evaluation summary."""
    summary = _dict_or_empty(raw_payload.get("summary"))
    run = _dict_or_empty(raw_payload.get("run"))
    rag = _dict_or_empty(raw_payload.get("rag"))
    rag_summary = _dict_or_empty(rag.get("summary"))
    resume_metrics = _dict_or_empty(summary.get("resume_metrics"))
    categories = _dict_or_empty(summary.get("categories"))

    return {
        "available": True,
        "path": str(summary_path),
        "run": run,
        "summary": summary,
        "resume_metrics": resume_metrics,
        "categories": categories,
        "rag": rag_summary,
        "dashboard": build_eval_dashboard(run, summary, resume_metrics, categories, rag_summary),
        "cases": raw_payload.get("cases", []) if isinstance(raw_payload.get("cases"), list) else [],
        "failed_cases": summary.get("failed_cases", []),
        "message": "evaluation summary loaded",
    }


def build_eval_unavailable_payload(message: str, *, summary_path: Path) -> dict[str, Any]:
    """Build the stable unavailable payload for missing or invalid eval summaries."""
    return {
        "available": False,
        "path": str(summary_path),
        "run": None,
        "summary": None,
        "resume_metrics": {},
        "categories": {},
        "rag": {},
        "dashboard": {
            "generated_at": None,
            "scope": "",
            "command": "",
            "artifacts": {},
            "metrics": [],
        },
        "cases": [],
        "failed_cases": [],
        "message": message,
    }


def build_adapter_unavailable_payload(message: str, *, adapter_path: Path) -> dict[str, Any]:
    """Build the stable unavailable payload for adapter verification status."""
    return {
        "available": False,
        "path": str(adapter_path),
        "status": "missing",
        "checks": [],
        "data_sources": [],
        "failed_tools": [],
        "duration_ms": 0,
        "summary": message,
        "message": message,
    }


def build_eval_dashboard(
    run: dict[str, Any],
    summary: dict[str, Any],
    resume_metrics: dict[str, Any],
    categories: dict[str, Any],
    rag_summary: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical dashboard metrics shown by the static frontend."""
    risk = _dict_or_empty(categories.get("risk"))
    rag_category = _dict_or_empty(categories.get("rag"))
    diagnostic_chain = _dict_or_empty(categories.get("diagnostic_chain"))

    metrics = [
        _metric(
            "total_cases",
            "总用例数",
            summary.get("overall_case_count"),
            "integer",
            "AIOps 与 RAG 离线评测用例总数。",
            "summary.overall_case_count",
        ),
        _metric(
            "overall_pass_rate",
            "总通过率",
            summary.get("overall_pass_rate"),
            "percent",
            "全部离线用例的通过比例。",
            "summary.overall_pass_rate",
        ),
        _metric(
            "aiops_pass_rate",
            "AIOps 用例通过率",
            _first_present(resume_metrics.get("aiops_pass_rate"), summary.get("pass_rate")),
            "percent",
            "故障诊断、工具、风险和报告链路的离线通过率。",
            "summary.resume_metrics.aiops_pass_rate",
        ),
        _metric(
            "rag_pass_rate",
            "RAG 用例通过率",
            rag_summary.get("pass_rate"),
            "percent",
            "Runbook 检索评测用例的通过比例。",
            "rag.summary.pass_rate",
        ),
        _metric(
            "root_cause_hit_rate",
            "根因识别通过率",
            resume_metrics.get("root_cause_hit_rate"),
            "percent",
            "报告根因是否命中评测集期望关键词。",
            "summary.resume_metrics.root_cause_hit_rate",
        ),
        _metric(
            "tool_hit_rate",
            "工具选择通过率",
            resume_metrics.get("tool_hit_rate"),
            "percent",
            "Planner 是否选择了期望诊断工具。",
            "summary.resume_metrics.tool_hit_rate",
        ),
        _metric(
            "approval_recall",
            "审批触发通过率",
            resume_metrics.get("approval_recall"),
            "percent",
            "需要人工确认的动作是否进入审批链路。",
            "summary.resume_metrics.approval_recall",
        ),
        _metric(
            "forbidden_action_block_rate",
            "禁止动作识别通过率",
            _first_present(
                resume_metrics.get("forbidden_action_block_rate"),
                risk.get("forbidden_action_block_rate"),
            ),
            "percent",
            "危险动作是否被风险控制层阻断。",
            "summary.resume_metrics.forbidden_action_block_rate",
        ),
        _metric(
            "rag_citation_pass_rate",
            "RAG 引用通过率",
            _first_present(
                resume_metrics.get("rag_citation_coverage_rate"),
                rag_summary.get("citation_coverage_rate"),
                rag_category.get("citation_coverage_rate"),
            ),
            "percent",
            "成功回答是否带有 source_file + chunk_id 引用。",
            "summary.resume_metrics.rag_citation_coverage_rate",
        ),
        _metric(
            "rag_no_answer_rejection_rate",
            "RAG 拒答通过率",
            _first_present(
                resume_metrics.get("rag_no_answer_rejection_rate"),
                rag_summary.get("no_answer_rejection_rate"),
            ),
            "percent",
            "无可靠资料时是否拒答而不是强答。",
            "summary.resume_metrics.rag_no_answer_rejection_rate",
        ),
        _metric(
            "p95_latency_ms",
            "p95 延迟",
            _first_present(resume_metrics.get("p95_latency_ms"), summary.get("p95_latency_ms")),
            "duration_ms",
            "离线评测单 case 执行耗时的 p95。",
            "summary.resume_metrics.p95_latency_ms",
        ),
        _metric(
            "diagnostic_trace_completeness",
            "Trace 完整性",
            _first_present(
                resume_metrics.get("diagnostic_trace_completeness"),
                diagnostic_chain.get("trace_completeness"),
            ),
            "percent",
            "离线诊断链路是否形成 trace_id、工具调用、证据和报告闭环。",
            "summary.categories.diagnostic_chain.trace_completeness",
        ),
        _metric(
            "diagnostic_root_cause_hit",
            "假设根因命中",
            _first_present(
                resume_metrics.get("diagnostic_root_cause_hit"),
                diagnostic_chain.get("root_cause_hit"),
            ),
            "percent",
            "根因假设排序是否覆盖 case 期望关键词；仅代表离线诊断链路评测。",
            "summary.categories.diagnostic_chain.root_cause_hit",
        ),
    ]

    return {
        "generated_at": run.get("ended_at") or run.get("started_at"),
        "scope": run.get("evaluation_scope", ""),
        "command": run.get("command", ""),
        "artifacts": _dict_or_empty(run.get("artifacts")),
        "metrics": metrics,
    }


def _metric(
    key: str,
    label: str,
    value: Any,
    value_type: str,
    description: str,
    source: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "value_type": value_type,
        "description": description,
        "source": source,
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
