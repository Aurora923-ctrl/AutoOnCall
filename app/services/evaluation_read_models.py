"""Read models for offline evaluation dashboards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.feedback import EvalBacklogItem
from scripts.eval.eval_environment import assess_eval_artifact_staleness


def build_eval_summary_payload(
    raw_payload: dict[str, Any],
    *,
    summary_path: Path,
    backlog_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the frontend/API payload for an available evaluation summary."""
    summary = _dict_or_empty(raw_payload.get("summary"))
    run = _dict_or_empty(raw_payload.get("run"))
    rag = _dict_or_empty(raw_payload.get("rag"))
    rag_summary = _dict_or_empty(rag.get("summary"))
    resume_metrics = _dict_or_empty(summary.get("resume_metrics"))
    categories = _dict_or_empty(summary.get("categories"))
    backlog = build_eval_backlog_summary(backlog_payload)
    artifact_status = assess_eval_artifact_staleness(run)

    return {
        "available": not artifact_status["stale"],
        "path": summary_path.name,
        "artifact": summary_path.name,
        "run": run,
        "summary": summary,
        "resume_metrics": resume_metrics,
        "categories": categories,
        "rag": rag_summary,
        "dashboard": build_eval_dashboard(run, summary, resume_metrics, categories, rag_summary),
        "cases": raw_payload.get("cases", []) if isinstance(raw_payload.get("cases"), list) else [],
        "failed_cases": summary.get("failed_cases", []),
        "eval_backlog": backlog,
        "artifact_status": artifact_status,
        "stale": artifact_status["stale"],
        "message": (
            "evaluation summary is stale"
            if artifact_status["stale"]
            else "evaluation summary loaded"
        ),
    }


def build_eval_unavailable_payload(message: str, *, summary_path: Path) -> dict[str, Any]:
    """Build the stable unavailable payload for missing or invalid eval summaries."""
    return {
        "available": False,
        "path": summary_path.name,
        "artifact": summary_path.name,
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
        "eval_backlog": build_eval_backlog_summary(None),
        "artifact_status": {
            "stale": True,
            "reasons": ["artifact_unavailable"],
            "generated_fingerprint": "",
            "current_fingerprint": "",
        },
        "stale": True,
        "message": message,
    }


def build_interview_scorecard_payload(
    raw_payload: dict[str, Any],
    *,
    scorecard_path: Path,
) -> dict[str, Any]:
    """Expose one-run interview scorecard data without merging current artifacts."""
    run = _dict_or_empty(raw_payload.get("run"))
    summary = _dict_or_empty(raw_payload.get("summary"))
    modules = raw_payload.get("modules")
    module_rows = modules if isinstance(modules, list) else []
    run_id = str(run.get("run_id") or "")
    environment = _dict_or_empty(run.get("environment"))
    artifact_status = assess_eval_artifact_staleness(
        {"environment": environment} if environment else {}
    )
    invalid_run_rows = [
        str(item.get("key") or "unknown")
        for item in module_rows
        if isinstance(item, dict) and str(item.get("run_id") or "") != run_id
    ]
    return {
        "available": bool(run_id) and not invalid_run_rows and not artifact_status["stale"],
        "path": scorecard_path.name,
        "artifact": scorecard_path.name,
        "run": run,
        "summary": summary,
        "modules": module_rows,
        "single_run_valid": bool(run_id) and not invalid_run_rows,
        "invalid_run_modules": invalid_run_rows,
        "artifact_status": artifact_status,
        "stale": artifact_status["stale"],
        "message": (
            "interview scorecard loaded"
            if run_id and not invalid_run_rows and not artifact_status["stale"]
            else (
                "interview scorecard is stale"
                if artifact_status["stale"]
                else "interview scorecard contains mixed or missing run ids"
            )
        ),
    }


def build_interview_scorecard_unavailable_payload(
    message: str,
    *,
    scorecard_path: Path,
) -> dict[str, Any]:
    """Return a stable unavailable scorecard shape."""
    return {
        "available": False,
        "path": scorecard_path.name,
        "artifact": scorecard_path.name,
        "run": None,
        "summary": None,
        "modules": [],
        "single_run_valid": False,
        "invalid_run_modules": [],
        "artifact_status": {
            "stale": True,
            "reasons": ["artifact_unavailable"],
            "generated_fingerprint": "",
            "current_fingerprint": "",
        },
        "stale": True,
        "message": message,
    }


def build_ragas_summary_payload(
    raw_payload: dict[str, Any],
    *,
    summary_path: Path,
) -> dict[str, Any]:
    """Build the frontend/API payload for an available RAGAS quality summary."""
    run = _dict_or_empty(raw_payload.get("run"))
    summary = _dict_or_empty(raw_payload.get("summary"))
    thresholds = _dict_or_empty(raw_payload.get("thresholds"))
    quality_contract = _dict_or_empty(raw_payload.get("quality_contract"))
    case_scores = raw_payload.get("case_scores", [])
    cases = case_scores if isinstance(case_scores, list) else []
    failed_cases = _failed_ragas_cases(summary, cases)
    dashboard = build_ragas_dashboard(run, summary, thresholds)
    artifact_status = assess_eval_artifact_staleness(run)
    return {
        "available": not artifact_status["stale"],
        "path": summary_path.name,
        "artifact": summary_path.name,
        "run": run,
        "summary": summary,
        "thresholds": thresholds,
        "quality_contract": quality_contract,
        "dashboard": dashboard,
        "case_scores": cases,
        "failed_cases": failed_cases,
        "artifact_status": artifact_status,
        "stale": artifact_status["stale"],
        "message": (
            "RAGAS quality summary is stale"
            if artifact_status["stale"]
            else "RAGAS quality summary loaded"
        ),
    }


def build_ragas_unavailable_payload(message: str, *, summary_path: Path) -> dict[str, Any]:
    """Build the stable unavailable payload for missing or invalid RAGAS summaries."""
    return {
        "available": False,
        "path": summary_path.name,
        "artifact": summary_path.name,
        "run": None,
        "summary": None,
        "thresholds": {},
        "quality_contract": {},
        "dashboard": {
            "generated_at": None,
            "scope": "",
            "command": "",
            "artifacts": {},
            "metrics": [],
            "profile": "",
        },
        "case_scores": [],
        "failed_cases": [],
        "invalid_items": [],
        "artifact_status": {
            "stale": True,
            "reasons": ["artifact_unavailable"],
            "generated_fingerprint": "",
            "current_fingerprint": "",
        },
        "stale": True,
        "message": message,
    }


def build_eval_backlog_summary(backlog_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return compact eval-backlog counters for dashboards."""
    payload = backlog_payload if isinstance(backlog_payload, dict) else {}
    raw_items = payload.get("items", [])
    items: list[dict[str, Any]] = []
    invalid_items: list[dict[str, Any]] = []
    if isinstance(raw_items, list):
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                invalid_items.append({"index": index, "reason": "item is not an object"})
                continue
            try:
                items.append(EvalBacklogItem.model_validate(raw_item).model_dump(mode="json"))
            except Exception as exc:
                invalid_items.append({"index": index, "reason": f"{type(exc).__name__}: {exc}"})
    summary = _summarize_backlog_items(items)
    return {
        "available": bool(payload),
        "summary": summary,
        "items": items,
        "invalid_items": invalid_items,
    }


def _failed_ragas_cases(
    summary: dict[str, Any],
    case_scores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use case-level pass flags when a RAGAS summary omits failed_cases."""
    failed = summary.get("failed_cases")
    if isinstance(failed, list) and failed:
        return [item for item in failed if isinstance(item, dict)]
    return [
        item
        for item in case_scores
        if isinstance(item, dict)
        and (
            item.get("passed") is False
            or str(item.get("status") or "").lower() in {"failed", "error"}
            or bool(item.get("failed_metrics"))
        )
    ]


def build_ragas_dashboard(
    run: dict[str, Any],
    summary: dict[str, Any],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Build metrics for the optional RAGAS quality dashboard."""
    profile = str(run.get("metric_profile") or "")
    judge_model = (
        "not_required_for_id_smoke" if profile == "id-smoke" else str(run.get("judge_model") or "")
    )
    embedding_model = (
        "not_required_for_id_smoke"
        if profile == "id-smoke"
        else str(run.get("embedding_model") or "")
    )
    metrics = [
        _metric(
            "ragas_pass_rate",
            "RAGAS case pass rate",
            summary.get("pass_rate"),
            "percent",
            "Fixed-case RAG answer quality pass rate for the selected RAGAS profile.",
            "summary.pass_rate",
        ),
        _metric(
            "ragas_core_pass_rate",
            "Core RAGAS pass rate",
            summary.get("core_case_pass_rate"),
            "percent",
            "Pass rate for interview-critical RAGAS cases.",
            "summary.core_case_pass_rate",
        ),
        _metric(
            "ragas_id_recall",
            "ID context recall",
            summary.get("id_context_recall_avg"),
            "percent",
            "Whether retrieved context ids cover the expected trusted sources.",
            "summary.id_context_recall_avg",
        ),
        _metric(
            "ragas_id_precision",
            "ID context precision",
            summary.get("id_context_precision_avg"),
            "percent",
            "How much extra context appears in Top-K retrieval; reported for smoke profile.",
            "summary.id_context_precision_avg",
        ),
        _metric(
            "ragas_actionability",
            "OnCall actionability",
            summary.get("oncall_actionability_avg"),
            "percent",
            "Business gate requiring incident domain, evidence language, and bounded actions.",
            "summary.oncall_actionability_avg",
        ),
        _metric(
            "ragas_refusal_boundary",
            "Refusal boundary",
            summary.get("refusal_boundary_rate"),
            "percent",
            "Out-of-scope questions must refuse with explicit trusted-source language.",
            "summary.refusal_boundary_rate",
        ),
        _metric(
            "ragas_faithfulness",
            "Faithfulness",
            summary.get("faithfulness_avg"),
            "percent",
            "LLM-as-judge support by retrieved context; populated by the full profile.",
            "summary.faithfulness_avg",
        ),
        _metric(
            "ragas_relevancy",
            "Response relevancy",
            summary.get("response_relevancy_avg"),
            "percent",
            "LLM-as-judge answer focus; populated by the full profile.",
            "summary.response_relevancy_avg",
        ),
    ]
    return {
        "generated_at": run.get("ended_at") or run.get("started_at"),
        "scope": run.get("evaluation_scope", ""),
        "command": _ragas_command_hint(run),
        "artifacts": _dict_or_empty(run.get("artifacts")),
        "metrics": metrics,
        "profile": profile,
        "answer_source": run.get("answer_source", ""),
        "judge_model": judge_model,
        "embedding_model": embedding_model,
        "thresholds": thresholds,
        "id_metric_execution": _dict_or_empty(run.get("id_metric_execution")),
        "metric_coverage": _dict_or_empty(summary.get("metric_coverage")),
    }


def _ragas_command_hint(run: dict[str, Any]) -> str:
    profile = str(run.get("metric_profile") or "id-smoke")
    answer_source = str(run.get("answer_source") or "product-offline")
    cases_path = str(run.get("cases_path") or "eval/rag_cases.yaml")
    docs_dir = str(run.get("docs_dir") or "docs/knowledge-base")
    return (
        "python scripts/eval/eval_ragas_cases.py "
        f"--cases {cases_path} --docs-dir {docs_dir} "
        f"--answer-source {answer_source} --metrics-profile {profile}"
    )


def build_adapter_unavailable_payload(message: str, *, adapter_path: Path) -> dict[str, Any]:
    """Build the stable unavailable payload for adapter verification status."""
    return {
        "available": False,
        "path": adapter_path.name,
        "artifact": adapter_path.name,
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
            "rag_retrieval_citation_metadata_rate",
            "RAG 检索引用元数据率",
            _first_present(
                resume_metrics.get("rag_citation_coverage_rate"),
                rag_summary.get("citation_coverage_rate"),
                rag_category.get("citation_coverage_rate"),
            ),
            "percent",
            "相关检索结果是否具备 source_file + chunk_id；不代表生成答案实际引用。",
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


def _summarize_backlog_items(items: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": 0,
        "by_target": {},
        "by_category": {},
        "by_priority": {},
        "by_review_status": {},
        "by_eval_file": {},
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        summary["total"] = int(summary["total"]) + 1
        _increment(summary["by_target"], item.get("target"))
        _increment(summary["by_category"], item.get("category"))
        _increment(summary["by_priority"], item.get("priority"))
        _increment(summary["by_review_status"], item.get("review_status"))
        _increment(summary["by_eval_file"], item.get("suggested_eval_file"))
    return summary


def _increment(bucket: dict[str, int], value: Any) -> None:
    key = str(value or "").strip()
    if key:
        bucket[key] = bucket.get(key, 0) + 1


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
