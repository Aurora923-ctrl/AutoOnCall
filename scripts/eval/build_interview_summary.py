"""Build a single interview-facing summary from eval artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.eval_environment import (  # noqa: E402
    assess_eval_artifact_staleness,
    collect_eval_environment,
    provenance_markdown_lines,
)

DEFAULT_LIVE_SUMMARY = REPO_ROOT / "logs" / "live_golden_eval_summary_current.json"
DEFAULT_RAG_SUMMARY = REPO_ROOT / "logs" / "rag_eval_summary_current.json"
DEFAULT_RAGAS_SUMMARY = REPO_ROOT / "logs" / "ragas_eval_summary.json"
DEFAULT_ADAPTER_SUMMARY = REPO_ROOT / "logs" / "full_stack_adapter_verification.json"
DEFAULT_MILVUS_SUMMARY = REPO_ROOT / "logs" / "milvus_multisource_verification.json"
DEFAULT_CHANGE_SUMMARY = REPO_ROOT / "logs" / "change_eval_summary.json"
DEFAULT_REPLANNER_SUMMARY = REPO_ROOT / "logs" / "replanner_eval_summary.json"
DEFAULT_OUTPUT_JSON = REPO_ROOT / "logs" / "interview_eval_summary.json"
DEFAULT_OUTPUT_MD = REPO_ROOT / "logs" / "interview_eval_summary.md"
DEFAULT_BENCHMARK_LATEST = REPO_ROOT / "logs" / "benchmarks" / "latest.json"

CORE_CASE_IDS = ["redis_maxclients_timeout", "mysql_slow_query_latency", "pod_crashloop"]
NEGATIVE_BOUNDARY_CASE_IDS = [
    "runbook_no_answer_rejection",
    "k8s_permission_denied_incomplete_report",
]
CORE_MODULES = ["adapter_verification", "live_aiops_eval", "rag_eval"]
INTERVIEW_GATE_MODULES = [
    "adapter_verification",
    "live_aiops_eval",
    "rag_eval",
    "safe_change_eval",
    "replanner_eval",
]

SCORECARD_MODULES = [
    ("baseline", "Baseline provenance", None),
    ("knowledge_quality", "Knowledge quality", "knowledge_quality"),
    ("rag_retrieval", "RAG retrieval", "rag"),
    ("answer_quality", "Answer quality", "ragas"),
    ("agent_rca", "Agent RCA", "aiops"),
    ("security", "Safety", "safe_change"),
    ("performance", "Latency, token and cost", "performance"),
    ("capacity", "Concurrency capacity", "load_test"),
    ("controlled_fault", "Controlled fault", "controlled_faults"),
    ("production", "Production data", None),
]


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_latest_benchmark_manifest(pointer_path: Path = DEFAULT_BENCHMARK_LATEST) -> Path | None:
    """Resolve the latest benchmark manifest without mixing independent current artifacts."""
    pointer = load_json(pointer_path)
    if not pointer:
        return None
    value = str(pointer.get("manifest_json") or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def build_scorecard_from_manifest(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    """Build the stage-8 scorecard exclusively from one benchmark run."""
    run = manifest.get("run") if isinstance(manifest.get("run"), dict) else {}
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    modules = manifest.get("modules") if isinstance(manifest.get("modules"), list) else []
    module_by_id = {
        str(item.get("id")): item for item in modules if isinstance(item, dict) and item.get("id")
    }
    run_id = str(run.get("run_id") or manifest_path.parent.name)
    rows: list[dict[str, Any]] = []
    for key, label, source_id in SCORECARD_MODULES:
        if key == "baseline":
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "status": str(summary.get("status") or "missing"),
                    "baseline_status": str(summary.get("baseline_status") or "missing"),
                    "evidence_level": "local_live",
                    "run_id": run_id,
                    "sample_count": int(summary.get("module_count") or len(modules)),
                    "metrics": [
                        {
                            "key": "module_pass_rate",
                            "value": _nested(summary, "metrics", "module_pass_rate", "value"),
                            "confidence_interval": {},
                        }
                    ],
                    "failed_case_count": int(summary.get("failed_module_count") or 0),
                    "failed_cases": list(summary.get("official_block_reasons") or [])[:5],
                    "artifact_path": _artifact_path(str(manifest_path)),
                }
            )
            continue
        if key == "production":
            rows.append(_production_scorecard_module(run_id))
            continue
        module = module_by_id.get(str(source_id))
        rows.append(_scorecard_module(key, label, module, run_id))

    required_keys = {
        "baseline",
        "knowledge_quality",
        "rag_retrieval",
        "answer_quality",
        "agent_rca",
        "security",
    }
    available_rows = [row for row in rows if row["status"] not in {"missing", "not_enough_data"}]
    failed_rows = [row for row in available_rows if row["status"] not in {"passed", "official"}]
    required_failed_rows = [row for row in failed_rows if row["key"] in required_keys]
    return {
        "run": {
            "run_id": run_id,
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "command": run.get("command"),
            "manifest_path": _artifact_path(str(manifest_path)),
            "environment": run.get("environment", {}),
            "single_run_enforced": True,
        },
        "summary": {
            "status": "passed" if not required_failed_rows else "failed",
            "baseline_status": summary.get("baseline_status", "missing"),
            "official_baseline": bool(summary.get("official_baseline")),
            "module_count": len(rows),
            "available_module_count": len(available_rows),
            "passed_module_count": sum(
                1 for row in available_rows if row["status"] in {"passed", "official"}
            ),
            "failed_module_count": len(failed_rows),
            "missing_optional_module_count": sum(
                1
                for row in rows
                if row["key"] not in required_keys
                and row["status"] in {"missing", "not_enough_data"}
            ),
            "production_status": "not_enough_data",
            "production_boundary": (
                "No production incident sample is present in this run. Controlled-fault "
                "latency or recovery metrics must not be presented as production MTTD/MTTR."
            ),
        },
        "modules": rows,
    }


def _scorecard_module(
    key: str,
    label: str,
    module: dict[str, Any] | None,
    run_id: str,
) -> dict[str, Any]:
    if not module:
        return {
            "key": key,
            "label": label,
            "status": "missing",
            "evidence_level": _default_evidence_level(key),
            "run_id": run_id,
            "sample_count": 0,
            "metrics": [],
            "failed_case_count": 0,
            "failed_cases": [],
            "artifact_path": "",
        }
    summary = module.get("summary") if isinstance(module.get("summary"), dict) else {}
    artifact_path = str(module.get("json_path") or "")
    artifact = load_json(REPO_ROOT / artifact_path) if artifact_path else None
    failed_cases = _failed_cases(summary, artifact)
    return {
        "key": key,
        "label": label,
        "status": str(module.get("status") or "missing"),
        "evidence_level": str(module.get("evidence_level") or _default_evidence_level(key)),
        "run_id": run_id,
        "sample_count": _sample_count(summary, artifact),
        "metrics": _scorecard_metrics(summary),
        "failed_case_count": len(failed_cases),
        "failed_cases": failed_cases[:5],
        "artifact_path": artifact_path,
    }


def _production_scorecard_module(run_id: str) -> dict[str, Any]:
    return {
        "key": "production",
        "label": "Production data",
        "status": "not_enough_data",
        "evidence_level": "production",
        "run_id": run_id,
        "sample_count": 0,
        "metrics": [],
        "failed_case_count": 0,
        "failed_cases": [],
        "artifact_path": "",
        "collection_fields": [
            "alert_time",
            "diagnosis_start_time",
            "first_useful_diagnosis_time",
            "resolve_time",
            "human_confirmed_root_cause",
            "recommendation_accepted",
            "recommendation_executed",
            "recovered_after_execution",
        ],
    }


def _default_evidence_level(key: str) -> str:
    if key == "controlled_fault":
        return "controlled_fault"
    if key in {"performance", "capacity", "knowledge_quality"}:
        return "local_live"
    return "offline_fixture"


def _sample_count(summary: dict[str, Any], artifact: dict[str, Any] | None) -> int:
    for key in (
        "sample_count",
        "case_count",
        "overall_case_count",
        "asset_count",
        "request_count",
        "experiment_count",
        "check_count",
    ):
        value = summary.get(key)
        if isinstance(value, int | float):
            return int(value)
    cases = (artifact or {}).get("cases")
    return len(cases) if isinstance(cases, list) else 0


def _failed_cases(
    summary: dict[str, Any],
    artifact: dict[str, Any] | None,
) -> list[Any]:
    raw = summary.get("failed_cases") or summary.get("failed_checks")
    if isinstance(raw, list):
        return raw
    cases = (artifact or {}).get("cases")
    if isinstance(cases, list):
        return [
            {"id": case.get("id"), "failure_reasons": case.get("failure_reasons", {})}
            for case in cases
            if isinstance(case, dict) and case.get("passed") is False
        ]
    return []


def _scorecard_metrics(summary: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    structured = summary.get("metrics")
    if isinstance(structured, dict):
        for key, value in structured.items():
            if isinstance(value, dict) and "value" in value:
                metrics.append(
                    {
                        "key": str(key),
                        "value": value.get("value"),
                        "numerator": value.get("numerator"),
                        "denominator": value.get("denominator"),
                        "sample_count": value.get("sample_count"),
                        "confidence_interval": value.get("confidence_interval", {}),
                    }
                )
    for key in (
        "pass_rate",
        "overall_pass_rate",
        "recall_at_k",
        "recall_at_3",
        "precision_at_3",
        "mrr",
        "ndcg_at_3",
        "citation_coverage_rate",
        "core_case_pass_rate",
        "refusal_boundary_rate",
        "p95_latency_ms",
    ):
        value = summary.get(key)
        if isinstance(value, int | float) and not any(item["key"] == key for item in metrics):
            metrics.append({"key": key, "value": value, "confidence_interval": {}})
    return metrics[:8]


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def render_scorecard_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# AutoOnCall Interview Scorecard",
        "",
        f"- Run ID: `{payload['run']['run_id']}`",
        f"- Manifest: `{payload['run']['manifest_path']}`",
        f"- Single run enforced: `{payload['run']['single_run_enforced']}`",
        f"- Scorecard status: `{summary['status']}`",
        f"- Baseline status: `{summary['baseline_status']}`",
        f"- Production: `{summary['production_status']}`",
        "",
        "| Module | Evidence | Samples | Status | Failed cases | Artifact |",
        "| --- | --- | ---: | --- | ---: | --- |",
    ]
    for module in payload["modules"]:
        lines.append(
            f"| {module['label']} | `{module['evidence_level']}` | "
            f"{module['sample_count']} | `{module['status']}` | "
            f"{module['failed_case_count']} | `{module['artifact_path'] or '-'}` |"
        )
    lines.extend(
        [
            "",
            "## Production Boundary",
            "",
            summary["production_boundary"],
            "",
            "## Failure Samples",
            "",
        ]
    )
    for module in payload["modules"]:
        if module["failed_cases"]:
            lines.append(
                f"- `{module['key']}`: `{json.dumps(module['failed_cases'][0], ensure_ascii=False)}`"
            )
    if not any(module["failed_cases"] for module in payload["modules"]):
        lines.append("- No failed case is present in the available modules for this run.")
    lines.append("")
    return "\n".join(lines)


def _ratio_percent(value: Any) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "-"


def _passed_text(passed: Any, total: Any) -> str:
    if passed is None or total is None:
        return "missing"
    return f"{passed}/{total} passed"


def _case_by_id(payload: dict[str, Any] | None, case_id: str) -> dict[str, Any]:
    for case in (payload or {}).get("cases", []):
        if case.get("id") == case_id:
            return case
    return {}


def _module_status(name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "name": name,
            "status": "missing",
            "passed": False,
            "case_count": 0,
            "passed_count": 0,
            "pass_rate": 0.0,
            "stale": True,
            "stale_reasons": ["artifact_missing"],
        }

    summary = payload.get("summary", {})
    run = payload.get("run")
    artifact_status = (
        assess_eval_artifact_staleness(run)
        if isinstance(run, dict) and run
        else {
            "stale": False,
            "reasons": [],
        }
    )
    status = payload.get("status")
    if status in {"passed", "failed"}:
        passed = status == "passed" and not artifact_status["stale"]
        return {
            "name": name,
            "status": "stale" if artifact_status["stale"] else status,
            "passed": passed,
            "case_count": len(payload.get("checks", [])),
            "passed_count": sum(1 for check in payload.get("checks", []) if check.get("passed")),
            "pass_rate": 1.0 if passed else 0.0,
            "stale": artifact_status["stale"],
            "stale_reasons": artifact_status["reasons"],
        }

    case_count = int(summary.get("overall_case_count") or summary.get("case_count") or 0)
    passed_count = int(summary.get("overall_passed_count") or summary.get("passed_count") or 0)
    all_passed = bool(summary.get("all_passed", passed_count == case_count and case_count > 0))
    pass_rate = float(summary.get("overall_pass_rate") or summary.get("pass_rate") or 0.0)
    passed = all_passed and not artifact_status["stale"]
    return {
        "name": name,
        "status": "stale" if artifact_status["stale"] else ("passed" if all_passed else "failed"),
        "passed": passed,
        "case_count": case_count,
        "passed_count": passed_count,
        "pass_rate": pass_rate,
        "stale": artifact_status["stale"],
        "stale_reasons": artifact_status["reasons"],
    }


def build_summary(
    *,
    live_payload: dict[str, Any] | None,
    rag_payload: dict[str, Any] | None,
    adapter_payload: dict[str, Any] | None,
    milvus_payload: dict[str, Any] | None = None,
    ragas_payload: dict[str, Any] | None = None,
    change_payload: dict[str, Any] | None = None,
    replanner_payload: dict[str, Any] | None = None,
    source_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    live_status = _module_status("live_aiops_eval", live_payload)
    rag_status = _module_status("rag_eval", rag_payload)
    adapter_status = _module_status("adapter_verification", adapter_payload)
    change_status = _module_status("safe_change_eval", change_payload)
    replanner_status = _module_status("replanner_eval", replanner_payload)

    cases = {case_id: _case_by_id(live_payload, case_id) for case_id in CORE_CASE_IDS}
    redis = cases["redis_maxclients_timeout"]
    mysql = cases["mysql_slow_query_latency"]
    k8s = cases["pod_crashloop"]

    modules = {
        "adapter_verification": adapter_status,
        "live_aiops_eval": live_status,
        "rag_eval": rag_status,
        "safe_change_eval": change_status,
        "replanner_eval": replanner_status,
    }
    core_passed = all(modules[name]["passed"] for name in CORE_MODULES)
    interview_gate_passed = all(modules[name]["passed"] for name in INTERVIEW_GATE_MODULES)

    return {
        "run": {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary_scope": (
                "interview-facing rollup; live AIOps and RAG evals remain separate "
                "source artifacts to avoid treating --skip-rag as a RAG result"
            ),
            "environment": collect_eval_environment(suite="interview_rollup"),
            "source_artifacts": (
                {
                    "live_aiops": str(DEFAULT_LIVE_SUMMARY.relative_to(REPO_ROOT)),
                    "rag": str(DEFAULT_RAG_SUMMARY.relative_to(REPO_ROOT)),
                    "ragas": str(DEFAULT_RAGAS_SUMMARY.relative_to(REPO_ROOT)),
                    "adapter_verification": str(DEFAULT_ADAPTER_SUMMARY.relative_to(REPO_ROOT)),
                    "milvus_multisource": str(DEFAULT_MILVUS_SUMMARY.relative_to(REPO_ROOT)),
                    "safe_change": str(DEFAULT_CHANGE_SUMMARY.relative_to(REPO_ROOT)),
                    "replanner": str(DEFAULT_REPLANNER_SUMMARY.relative_to(REPO_ROOT)),
                }
                if source_artifacts is None
                else source_artifacts
            ),
        },
        "summary": {
            "status": "passed" if interview_gate_passed else "failed",
            "core_modules_passed": core_passed,
            "interview_gate_passed": interview_gate_passed,
            "modules": modules,
            "portfolio_chains": {
                "redis_maxclients_timeout": _chain_summary(redis),
                "mysql_slow_query_latency": _chain_summary(mysql),
                "pod_crashloop": {
                    "status": "offline_regression_only",
                    "passed": bool(k8s.get("passed")),
                    "evidence_mode": k8s.get("evidence_mode", "offline_fixture"),
                    "source_boundary": k8s.get(
                        "source_boundary",
                        "K8s CrashLoop/OOMKilled is an offline golden regression case.",
                    ),
                },
            },
            "conclusion_alignment": _conclusion_alignment_summary([redis, mysql]),
            "negative_boundaries": _negative_boundary_summary(
                live_payload,
                NEGATIVE_BOUNDARY_CASE_IDS,
            ),
            "rag_metrics": _rag_metrics(rag_payload),
            "rag_mainline_support": _rag_mainline_support_summary(rag_payload),
            "ragas_quality": _ragas_quality(ragas_payload),
            "milvus_multisource": _milvus_summary(milvus_payload),
            "resume_metrics": _resume_metrics(
                live_payload=live_payload,
                rag_payload=rag_payload,
                ragas_payload=ragas_payload,
                change_status=change_status,
                replanner_status=replanner_status,
            ),
            "adapter_sources": {
                "status": (adapter_payload or {}).get("status", "missing"),
                "data_sources": (adapter_payload or {}).get("data_sources", []),
                "mock_fallback_detected": (adapter_payload or {}).get("mock_fallback_detected"),
                "missing_sources": (adapter_payload or {}).get("missing_sources", []),
                "failed_tools": (adapter_payload or {}).get("failed_tools", []),
                "golden_chains": (adapter_payload or {}).get("golden_chains", {}),
                "passed_golden_chain_count": (adapter_payload or {}).get(
                    "passed_golden_chain_count", 0
                ),
                "golden_chain_count": (adapter_payload or {}).get("golden_chain_count", 0),
            },
            "interview_boundaries": [
                "Redis/MySQL are live adapter golden chains backed by the local Docker stack.",
                "RAG eval is shown from its own retrieval summary, not from the --skip-rag AIOps run.",
                "RAGAS quality is optional and separate from retrieval recall; id-smoke is reproducible without a judge key.",
                "K8s CrashLoop/OOMKilled is an offline golden regression case in the default interview.",
                "Conclusion alignment is conclusion-level grounding, not full-sentence fact checking.",
            ],
        },
    }


def _chain_summary(case: dict[str, Any]) -> dict[str, Any]:
    metrics = case.get("metrics", {})
    return {
        "passed": bool(case.get("passed")),
        "evidence_mode": case.get("evidence_mode", ""),
        "tool_sources": case.get("tool_sources", {}),
        "required_live_sources_hit": bool(metrics.get("required_live_sources_hit")),
        "evidence_sufficiency_hit": bool(metrics.get("evidence_sufficiency_hit")),
        "runtime_vs_incident_boundary_hit": bool(metrics.get("runtime_vs_incident_boundary_hit")),
        "approval_boundary_hit": bool(metrics.get("approval_boundary_hit")),
    }


def _rag_metrics(payload: dict[str, Any] | None) -> dict[str, Any]:
    summary = (payload or {}).get("summary", {})
    return {
        "case_count": int(summary.get("case_count", 0) or 0),
        "passed_count": int(summary.get("passed_count", 0) or 0),
        "top_k": int(summary.get("top_k", 0) or 0),
        "recall_at_k": float(summary.get("recall_at_k", 0.0) or 0.0),
        "strict_recall_at_k": float(summary.get("strict_recall_at_k", 0.0) or 0.0),
        "mrr": float(summary.get("mrr", 0.0) or 0.0),
        "citation_coverage_rate": float(summary.get("citation_coverage_rate", 0.0) or 0.0),
        "no_answer_rejection_rate": float(summary.get("no_answer_rejection_rate", 0.0) or 0.0),
        "confusion_case_pass_rate": float(summary.get("confusion_case_pass_rate", 0.0) or 0.0),
    }


def _negative_boundary_summary(
    payload: dict[str, Any] | None,
    case_ids: list[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case_id in case_ids:
        case = _case_by_id(payload, case_id)
        expected_status = _expected_negative_status(case_id)
        rows.append(
            {
                "id": case_id,
                "passed": bool(case.get("passed")),
                "report_status": str(case.get("report_status") or "missing"),
                "expected_status": expected_status,
                "status_hit": str(case.get("report_status") or "") == expected_status,
                "runbook_rejected": bool(case.get("runbook_rejected")),
                "failed_tools": [str(item) for item in case.get("failed_tools") or []],
                "boundary": _negative_boundary_text(case_id),
            }
        )
    passed_count = sum(1 for row in rows if row["passed"] and row["status_hit"])
    return {
        "case_count": len(rows),
        "passed_count": passed_count,
        "all_passed": passed_count == len(rows) if rows else False,
        "cases": rows,
    }


def _expected_negative_status(case_id: str) -> str:
    if case_id == "runbook_no_answer_rejection":
        return "needs_human"
    if case_id == "k8s_permission_denied_incomplete_report":
        return "degraded"
    return "incomplete"


def _negative_boundary_text(case_id: str) -> str:
    if case_id == "runbook_no_answer_rejection":
        return "No trusted Runbook/history reference means remediation is under-grounded."
    if case_id == "k8s_permission_denied_incomplete_report":
        return "K8s RBAC denial prevents pod/event evidence, so metrics cannot prove K8s RCA."
    return "Under-evidenced RCA must not be reported as completed."


def _rag_mainline_support_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    expected = {
        "redis": {
            "cases": ["pdf_postmortem_loader_metadata", "redis_ticket_retry_loop_history"],
            "sources": ["redis_postmortem.pdf", "tickets.csv"],
            "role": "Redis RCA uses PDF postmortem plus ticket table history as knowledge/history backing.",
        },
        "mysql": {
            "cases": [
                "html_wiki_loader_heading",
                "xlsx_deploy_history_row_citation",
                "mysql_xlsx_rc4_remediation_history",
            ],
            "sources": ["payment_wiki.html", "tickets.xlsx"],
            "role": "MySQL RCA uses HTML wiki plus XLSX deploy/ticket rows as knowledge/history backing.",
        },
    }
    rows: list[dict[str, Any]] = []
    for chain, spec in expected.items():
        cases = [_case_by_id(payload, case_id) for case_id in spec["cases"]]
        present_cases = [case for case in cases if case]
        retrieved_sources = sorted(
            {str(source) for case in present_cases for source in case.get("retrieved_sources", [])}
        )
        expected_sources = [str(source) for source in spec["sources"]]
        source_hit = all(source in retrieved_sources for source in expected_sources)
        passed = bool(present_cases) and all(bool(case.get("passed")) for case in present_cases)
        rows.append(
            {
                "chain": chain,
                "passed": passed and source_hit,
                "case_ids": [str(case.get("id")) for case in present_cases],
                "expected_sources": expected_sources,
                "retrieved_sources": retrieved_sources,
                "role": spec["role"],
            }
        )
    passed_count = sum(1 for row in rows if row["passed"])
    return {
        "chain_count": len(rows),
        "passed_count": passed_count,
        "all_passed": passed_count == len(rows) if rows else False,
        "chains": rows,
    }


def _ragas_quality(payload: dict[str, Any] | None) -> dict[str, Any]:
    run = (payload or {}).get("run", {})
    summary = (payload or {}).get("summary", {})
    if not isinstance(run, dict):
        run = {}
    if not isinstance(summary, dict):
        summary = {}
    artifacts = run.get("artifacts") if isinstance(run.get("artifacts"), dict) else {}
    profile = str(run.get("metric_profile") or "")
    full_judge_metrics_run = profile == "full"
    return {
        "available": bool(payload),
        "status": str(summary.get("status") or "missing"),
        "profile": profile,
        "full_judge_metrics_run": full_judge_metrics_run,
        "full_judge_status": (
            "passed"
            if full_judge_metrics_run and str(summary.get("status") or "") == "passed"
            else "not_run"
        ),
        "answer_source": str(run.get("answer_source") or ""),
        "case_count": int(summary.get("case_count", 0) or 0),
        "core_case_count": int(summary.get("core_case_count", 0) or 0),
        "refusal_case_count": int(summary.get("refusal_case_count", 0) or 0),
        "passed_count": int(summary.get("passed_count", 0) or 0),
        "pass_rate": float(summary.get("pass_rate", 0.0) or 0.0),
        "core_case_pass_rate": float(summary.get("core_case_pass_rate", 0.0) or 0.0),
        "id_context_precision_avg": float(summary.get("id_context_precision_avg", 0.0) or 0.0),
        "id_context_recall_avg": float(summary.get("id_context_recall_avg", 0.0) or 0.0),
        "oncall_actionability_avg": float(summary.get("oncall_actionability_avg", 0.0) or 0.0),
        "refusal_boundary_rate": float(summary.get("refusal_boundary_rate", 0.0) or 0.0),
        "faithfulness_avg": float(summary.get("faithfulness_avg", 0.0) or 0.0),
        "response_relevancy_avg": float(summary.get("response_relevancy_avg", 0.0) or 0.0),
        "judge_model": str(run.get("judge_model") or ""),
        "embedding_model": str(run.get("embedding_model") or ""),
        "id_metric_execution": (
            run.get("id_metric_execution")
            if isinstance(run.get("id_metric_execution"), dict)
            else {}
        ),
        "metric_coverage": (
            summary.get("metric_coverage")
            if isinstance(summary.get("metric_coverage"), dict)
            else {}
        ),
        "artifacts": artifacts,
    }


def _conclusion_alignment_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        if not case:
            continue
        alignment = case.get("conclusion_alignment") or {}
        fields = alignment.get("fields") if isinstance(alignment, dict) else {}
        rows.extend(
            _alignment_rows(
                str(case.get("id") or "unknown"),
                fields if isinstance(fields, dict) else {},
            )
        )

    aligned_count = sum(1 for row in rows if row["aligned"])
    total_count = len(rows)
    return {
        "aligned_count": aligned_count,
        "total_count": total_count,
        "rate": aligned_count / total_count if total_count else 0.0,
        "fields": rows,
    }


def _alignment_rows(case_id: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root_cause = fields.get("root_cause")
    if isinstance(root_cause, dict):
        rows.append(_alignment_row(case_id, "root_cause", root_cause))

    key_findings = fields.get("key_findings")
    if isinstance(key_findings, list) and key_findings:
        evidence_ids: list[str] = []
        citation_count = 0
        aligned = True
        for item in key_findings:
            if not isinstance(item, dict):
                aligned = False
                continue
            aligned = aligned and bool(item.get("aligned"))
            evidence_ids.extend(str(value) for value in item.get("evidence_ids") or [])
            citation_count += len(item.get("citations") or [])
        rows.append(
            {
                "case_id": case_id,
                "field": "key_findings",
                "aligned": aligned,
                "evidence_ids": sorted(set(evidence_ids)),
                "citation_count": citation_count,
            }
        )

    remediation = fields.get("remediation_suggestion")
    if isinstance(remediation, dict):
        rows.append(_alignment_row(case_id, "remediation_suggestion", remediation))
    return rows


def _alignment_row(case_id: str, field_name: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "field": field_name,
        "aligned": bool(item.get("aligned")),
        "evidence_ids": [str(value) for value in item.get("evidence_ids") or []],
        "citation_count": len(item.get("citations") or []),
    }


def _milvus_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    summary = (payload or {}).get("summary", {})
    return {
        "status": str(summary.get("status") or "missing"),
        "inserted_chunks": int(summary.get("inserted_chunks", 0) or 0),
        "probe_count": int(summary.get("probe_count", 0) or 0),
        "passed_probe_count": int(summary.get("passed_probe_count", 0) or 0),
        "pass_rate": float(summary.get("pass_rate", 0.0) or 0.0),
        "source_counts": summary.get("source_counts", {}),
        "doc_type_counts": summary.get("doc_type_counts", {}),
    }


def _resume_metrics(
    *,
    live_payload: dict[str, Any] | None,
    rag_payload: dict[str, Any] | None,
    ragas_payload: dict[str, Any] | None,
    change_status: dict[str, Any],
    replanner_status: dict[str, Any],
) -> dict[str, Any]:
    live_summary = (live_payload or {}).get("summary", {})
    live_resume = live_summary.get("resume_metrics", {})
    if not isinstance(live_resume, dict):
        live_resume = {}
    rag = _rag_metrics(rag_payload)
    ragas = _ragas_quality(ragas_payload)
    return {
        "scope": (
            "resume-facing snapshot derived from current eval artifacts; use as regression "
            "evidence, not production accuracy"
        ),
        "aiops_case_count": int(live_resume.get("aiops_case_count") or 0),
        "aiops_pass_rate": float(live_resume.get("aiops_pass_rate") or 0.0),
        "p95_latency_ms": float(live_resume.get("p95_latency_ms") or 0.0),
        "root_cause_hit_rate": float(live_resume.get("root_cause_hit_rate") or 0.0),
        "tool_hit_rate": float(live_resume.get("tool_hit_rate") or 0.0),
        "report_generation_rate": float(live_resume.get("report_generation_rate") or 0.0),
        "approval_recall": float(live_resume.get("approval_recall") or 0.0),
        "forbidden_action_block_rate": float(live_resume.get("forbidden_action_block_rate") or 0.0),
        "evidence_sufficiency_rate": float(
            live_resume.get("diagnostic_evidence_sufficiency") or 0.0
        ),
        "runtime_boundary_rate": float(
            live_resume.get("diagnostic_runtime_vs_incident_boundary") or 0.0
        ),
        "rag_case_count": rag["case_count"],
        "rag_recall_at_k": rag["recall_at_k"],
        "rag_citation_coverage_rate": rag["citation_coverage_rate"],
        "rag_no_answer_rejection_rate": rag["no_answer_rejection_rate"],
        "ragas_available": bool(ragas["available"]),
        "ragas_profile": ragas["profile"],
        "ragas_pass_rate": ragas["pass_rate"],
        "ragas_oncall_actionability": ragas["oncall_actionability_avg"],
        "ragas_refusal_boundary_rate": ragas["refusal_boundary_rate"],
        "safe_change_pass_rate": float(change_status.get("pass_rate") or 0.0),
        "replanner_pass_rate": float(replanner_status.get("pass_rate") or 0.0),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    modules = summary["modules"]
    chains = summary["portfolio_chains"]
    rag = summary["rag_metrics"]
    rag_support = summary["rag_mainline_support"]
    ragas = summary["ragas_quality"]
    alignment = summary["conclusion_alignment"]
    negative = summary["negative_boundaries"]
    milvus = summary["milvus_multisource"]
    resume = summary["resume_metrics"]
    adapter = summary["adapter_sources"]

    lines = [
        "# AutoOnCall Interview Eval Summary",
        "",
        "## Rollup",
        "",
        f"- Status: `{summary['status']}`",
        f"- Generated at: `{payload['run']['generated_at']}`",
        "- Scope: interview-facing rollup of adapter verification, live AIOps golden eval, "
        "and standalone RAG retrieval eval.",
        *provenance_markdown_lines(payload["run"]["environment"]),
        "",
        "## Module Results",
        "",
        "| Module | Result | Pass rate | Notes |",
        "| --- | --- | ---: | --- |",
    ]
    for key, label in [
        ("adapter_verification", "Full stack adapter verification"),
        ("live_aiops_eval", "Live AIOps golden eval"),
        ("rag_eval", "RAG retrieval eval"),
        ("safe_change_eval", "Safe-change eval"),
        ("replanner_eval", "Replanner eval"),
    ]:
        item = modules[key]
        notes = _module_notes(key, item, adapter, rag)
        lines.append(
            f"| {label} | `{_passed_text(item['passed_count'], item['case_count'])}` "
            f"| {_ratio_percent(item['pass_rate'])} | {notes} |"
        )

    lines.extend(
        [
            "",
            "## Portfolio Chains",
            "",
            "| Chain | Status | Evidence mode | Required signals |",
            "| --- | --- | --- | --- |",
            _chain_row("Redis maxclients", chains["redis_maxclients_timeout"]),
            _chain_row("MySQL slow query", chains["mysql_slow_query_latency"]),
            (
                "| K8s CrashLoop/OOMKilled | "
                f"`{'PASS' if chains['pod_crashloop']['passed'] else 'CHECK'}` | "
                f"`{chains['pod_crashloop']['evidence_mode']}` | offline golden regression only |"
            ),
            "",
            "## RAG Snapshot",
            "",
            f"- RAG eval: `{rag['passed_count']}/{rag['case_count']} passed`",
            f"- recall@{rag['top_k']}: `{_ratio_percent(rag['recall_at_k'])}`",
            f"- strict recall@{rag['top_k']}: `{_ratio_percent(rag['strict_recall_at_k'])}`",
            f"- MRR: `{rag['mrr']:.2f}`",
            (
                "- retrieval citation metadata coverage: "
                f"`{_ratio_percent(rag['citation_coverage_rate'])}`"
            ),
            f"- no-answer rejection: `{_ratio_percent(rag['no_answer_rejection_rate'])}`",
            f"- confusion case pass: `{_ratio_percent(rag['confusion_case_pass_rate'])}`",
            "",
            "## RAG Mainline Support",
            "",
            f"- Mainline support: `{rag_support['passed_count']}/{rag_support['chain_count']} chains`",
            "- Purpose: prove multi-source RAG supports Redis/MySQL RCA, not just standalone QA.",
            "",
            "| Chain | Status | Expected sources | Retrieved sources | RCA role |",
            "| --- | --- | --- | --- | --- |",
            *_rag_support_rows(rag_support["chains"]),
            "",
            "## RAGAS Quality Snapshot",
            "",
            f"- RAGAS quality: `{ragas['passed_count']}/{ragas['case_count']} passed`",
            f"- profile: `{ragas['profile'] or 'missing'}`",
            f"- answer source: `{ragas['answer_source'] or 'missing'}`",
            f"- ID context recall: `{_ratio_percent(ragas['id_context_recall_avg'])}`",
            f"- ID context precision: `{_ratio_percent(ragas['id_context_precision_avg'])}`",
            f"- OnCall actionability: `{_ratio_percent(ragas['oncall_actionability_avg'])}`",
            f"- refusal boundary: `{_ragas_refusal_text(ragas)}`",
            f"- faithfulness/full judge: `{_ragas_full_metric_text(ragas, 'faithfulness_avg')}`",
            f"- response relevancy/full judge: "
            f"`{_ragas_full_metric_text(ragas, 'response_relevancy_avg')}`",
            f"- judge model: `{_ragas_judge_text(ragas)}`",
            (
                "- ID metric execution: "
                f"`{ragas['id_metric_execution'].get('engine', 'unknown')}/"
                f"{ragas['id_metric_execution'].get('status', 'unknown')}`"
            ),
            f"- metric coverage: `{_ragas_coverage_text(ragas)}`",
            "",
            "> RAGAS id-smoke is a reproducible answer-quality regression. "
            "Use `--metrics-profile full` when a judge key is available.",
            "",
            "## Conclusion Alignment",
            "",
            f"- conclusion_alignment_rate: "
            f"`{alignment['aligned_count']}/{alignment['total_count']} "
            f"({_ratio_percent(alignment['rate'])})`",
            "- Scope: Redis/MySQL main chains; fields are `root_cause`, "
            "`key_findings`, and `remediation_suggestion`.",
            "",
            "| Case | Field | Status | Evidence links | Citation count |",
            "| --- | --- | --- | --- | ---: |",
            *_alignment_markdown_rows(alignment["fields"]),
            "",
            "## Negative Boundaries",
            "",
            f"- Boundary cases: `{negative['passed_count']}/{negative['case_count']} passed`",
            "- Purpose: prove the system downgrades under-evidenced RCA instead of forcing a completed report.",
            "",
            "| Case | Status | Expected | Signals | Boundary |",
            "| --- | --- | --- | --- | --- |",
            *_negative_boundary_rows(negative["cases"]),
            "",
            "## Milvus Multi-Source Snapshot",
            "",
            f"- Status: `{milvus['status']}`",
            f"- Inserted chunks: `{milvus['inserted_chunks']}`",
            f"- Probe pass rate: `{milvus['passed_probe_count']}/{milvus['probe_count']}`",
            f"- Source files: `{', '.join(milvus['source_counts'].keys()) or 'missing'}`",
            "",
            "## Resume Metrics Snapshot",
            "",
            f"- Scope: {resume['scope']}",
            (
                f"- AIOps eval: `{resume['aiops_case_count']} cases`, "
                f"pass rate `{_ratio_percent(resume['aiops_pass_rate'])}`, "
                f"p95 latency `{resume['p95_latency_ms']:.2f} ms`"
            ),
            (
                f"- Diagnosis: root cause `{_ratio_percent(resume['root_cause_hit_rate'])}`, "
                f"tool hit `{_ratio_percent(resume['tool_hit_rate'])}`, "
                f"report generation `{_ratio_percent(resume['report_generation_rate'])}`"
            ),
            (
                f"- Safety and evidence: approval recall "
                f"`{_ratio_percent(resume['approval_recall'])}`, forbidden block "
                f"`{_ratio_percent(resume['forbidden_action_block_rate'])}`, "
                f"evidence sufficiency `{_ratio_percent(resume['evidence_sufficiency_rate'])}`, "
                f"runtime boundary `{_ratio_percent(resume['runtime_boundary_rate'])}`"
            ),
            (
                f"- RAG eval: `{resume['rag_case_count']} cases`, recall "
                f"`{_ratio_percent(resume['rag_recall_at_k'])}`, citation "
                f"`{_ratio_percent(resume['rag_citation_coverage_rate'])}`, no-answer "
                f"`{_ratio_percent(resume['rag_no_answer_rejection_rate'])}`"
            ),
            (
                f"- RAGAS quality: profile `{resume['ragas_profile'] or 'missing'}`, "
                f"pass rate `{_ratio_percent(resume['ragas_pass_rate'])}`, OnCall "
                f"actionability `{_ratio_percent(resume['ragas_oncall_actionability'])}`, "
                f"refusal boundary `{_ratio_percent(resume['ragas_refusal_boundary_rate'])}`"
            ),
            (
                f"- Change/Replanner gates: safe change "
                f"`{_ratio_percent(resume['safe_change_pass_rate'])}`, replanner "
                f"`{_ratio_percent(resume['replanner_pass_rate'])}`"
            ),
            "",
            "## Adapter Snapshot",
            "",
            f"- Adapter verification: `{adapter['status']}`",
            f"- Data sources: `{', '.join(adapter['data_sources']) or 'missing'}`",
            f"- mock_fallback_detected: `{adapter['mock_fallback_detected']}`",
            f"- missing_sources: `{adapter['missing_sources']}`",
            f"- failed_tools: `{adapter['failed_tools']}`",
            f"- Redis/MySQL golden chains: "
            f"`{adapter.get('passed_golden_chain_count', 0)}/{adapter.get('golden_chain_count', 0)}`",
            "",
            "| Chain | Status | Observed sources | Missing sources | Failed tools |",
            "| --- | --- | --- | --- | --- |",
            *_adapter_golden_chain_rows(adapter.get("golden_chains")),
            "",
            "## Interview Boundaries",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in summary["interview_boundaries"])
    lines.extend(
        [
            "",
            "## Source Artifacts",
            "",
            "- `logs/live_golden_eval_summary_current.md`: live AIOps run; usually uses `--skip-rag`.",
            "- `logs/rag_eval_summary_current.md`: standalone RAG retrieval result.",
            "- `logs/ragas_eval_summary.md`: optional RAGAS answer quality result.",
            "- `logs/milvus_multisource_verification.md`: Milvus storage proof for PDF/HTML/CSV/XLSX chunks.",
            "- `logs/change_eval_summary.md`: safe-change pre-check, dry-run, rollback, and manual-record gate.",
            "- `logs/replanner_eval_summary.md`: replanner decision and guardrail gate.",
            "- `logs/full_stack_adapter_verification.json`: real adapter source proof.",
            "",
        ]
    )
    return "\n".join(lines)


def _module_notes(
    key: str, item: dict[str, Any], adapter: dict[str, Any], rag: dict[str, Any]
) -> str:
    if item["status"] == "missing":
        return "artifact missing"
    if key == "adapter_verification":
        return f"mock_fallback_detected={adapter['mock_fallback_detected']}"
    if key == "rag_eval":
        return (
            f"recall@{rag['top_k']}={_ratio_percent(rag['recall_at_k'])}, "
            f"citation={_ratio_percent(rag['citation_coverage_rate'])}"
        )
    return item["status"]


def _ragas_refusal_text(ragas: dict[str, Any]) -> str:
    if int(ragas.get("refusal_case_count", 0) or 0) <= 0:
        return "not_covered"
    return _ratio_percent(ragas.get("refusal_boundary_rate"))


def _ragas_full_metric_text(ragas: dict[str, Any], key: str) -> str:
    if ragas.get("profile") != "full":
        return "not_run_in_id_smoke"
    return _ratio_percent(ragas.get(key))


def _ragas_judge_text(ragas: dict[str, Any]) -> str:
    if ragas.get("profile") != "full":
        return "not_required_for_id_smoke"
    return str(ragas.get("judge_model") or "missing")


def _ragas_coverage_text(ragas: dict[str, Any]) -> str:
    coverage = ragas.get("metric_coverage")
    if not isinstance(coverage, dict) or not coverage:
        return "not_reported"
    incomplete = []
    for metric, item in coverage.items():
        if not isinstance(item, dict):
            continue
        available = int(item.get("available_count", 0) or 0)
        expected = int(item.get("expected_count", 0) or 0)
        if available != expected:
            incomplete.append(f"{metric}={available}/{expected}")
    return "complete" if not incomplete else ", ".join(incomplete)


def _alignment_markdown_rows(rows: list[dict[str, Any]]) -> list[str]:
    rendered = []
    for row in rows:
        evidence_links = _compact_evidence_links(row["evidence_ids"])
        status = "aligned" if row["aligned"] else "needs_human"
        rendered.append(
            f"| `{row['case_id']}` | `{row['field']}` | `{status}` | "
            f"{evidence_links} | {row['citation_count']} |"
        )
    return rendered or ["| `missing` | `missing` | `needs_human` | - | 0 |"]


def _rag_support_rows(rows: list[dict[str, Any]]) -> list[str]:
    rendered = []
    for row in rows:
        status = "PASS" if row["passed"] else "CHECK"
        expected = ", ".join(row["expected_sources"]) or "-"
        retrieved = ", ".join(row["retrieved_sources"]) or "-"
        rendered.append(
            f"| `{row['chain']}` | `{status}` | {expected} | {retrieved} | {row['role']} |"
        )
    return rendered or ["| `missing` | `CHECK` | - | - | no RAG support artifact |"]


def _negative_boundary_rows(rows: list[dict[str, Any]]) -> list[str]:
    rendered = []
    for row in rows:
        signals = []
        if row["runbook_rejected"]:
            signals.append("runbook_rejected=true")
        if row["failed_tools"]:
            signals.append("failed_tools=" + ",".join(row["failed_tools"]))
        signal_text = "; ".join(signals) or "-"
        rendered.append(
            f"| `{row['id']}` | `{row['report_status']}` | "
            f"`{row['expected_status']}` | {signal_text} | {row['boundary']} |"
        )
    return rendered or [
        "| `missing` | `missing` | `incomplete` | - | no negative boundary artifact |"
    ]


def _adapter_golden_chain_rows(value: Any) -> list[str]:
    if not isinstance(value, dict) or not value:
        return ["| `missing` | `CHECK` | - | - | - |"]
    rows: list[str] = []
    for chain_name, raw_chain in sorted(value.items()):
        chain = raw_chain if isinstance(raw_chain, dict) else {}
        rows.append(
            "| "
            f"`{chain_name}` | "
            f"`{'PASS' if chain.get('passed') else 'CHECK'}` | "
            f"{', '.join(str(item) for item in chain.get('observed_sources') or []) or '-'} | "
            f"{', '.join(str(item) for item in chain.get('missing_sources') or []) or '-'} | "
            f"{', '.join(str(item) for item in chain.get('failed_tools') or []) or '-'} |"
        )
    return rows or ["| `missing` | `CHECK` | - | - | - |"]


def _compact_evidence_links(evidence_ids: list[str]) -> str:
    if not evidence_ids:
        return "-"
    visible = evidence_ids[:2]
    suffix = f" (+{len(evidence_ids) - len(visible)} more)" if len(evidence_ids) > 2 else ""
    return ", ".join(visible) + suffix


def _chain_row(label: str, chain: dict[str, Any]) -> str:
    signals = [
        f"required_live_sources_hit={chain['required_live_sources_hit']}",
        f"evidence_sufficiency_hit={chain['evidence_sufficiency_hit']}",
        f"runtime_vs_incident_boundary_hit={chain['runtime_vs_incident_boundary_hit']}",
        f"approval_boundary_hit={chain['approval_boundary_hit']}",
    ]
    return (
        f"| {label} | `{'PASS' if chain['passed'] else 'CHECK'}` | "
        f"`{chain['evidence_mode']}` | {'; '.join(signals)} |"
    )


def write_outputs(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")


def write_scorecard_outputs(
    payload: dict[str, Any],
    *,
    run_dir: Path,
    json_path: Path | None = None,
    md_path: Path | None = None,
) -> tuple[Path, Path]:
    scorecard_json = json_path or run_dir / "interview_scorecard.json"
    scorecard_md = md_path or run_dir / "interview_scorecard.md"
    scorecard_json.parent.mkdir(parents=True, exist_ok=True)
    scorecard_md.parent.mkdir(parents=True, exist_ok=True)
    scorecard_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    scorecard_md.write_text(render_scorecard_markdown(payload), encoding="utf-8")
    return scorecard_json, scorecard_md


def _artifact_path(value: str) -> str:
    path = Path(value)
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-summary", default=str(DEFAULT_LIVE_SUMMARY))
    parser.add_argument("--rag-summary", default=str(DEFAULT_RAG_SUMMARY))
    parser.add_argument("--ragas-summary", default=str(DEFAULT_RAGAS_SUMMARY))
    parser.add_argument("--adapter-summary", default=str(DEFAULT_ADAPTER_SUMMARY))
    parser.add_argument("--milvus-summary", default=str(DEFAULT_MILVUS_SUMMARY))
    parser.add_argument("--change-summary", default=str(DEFAULT_CHANGE_SUMMARY))
    parser.add_argument("--replanner-summary", default=str(DEFAULT_REPLANNER_SUMMARY))
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    parser.add_argument(
        "--benchmark-manifest",
        default="",
        help="Build the stage-8 scorecard from this single benchmark manifest.",
    )
    parser.add_argument("--scorecard-json", default="")
    parser.add_argument("--scorecard-md", default="")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = (
        Path(args.benchmark_manifest)
        if args.benchmark_manifest
        else resolve_latest_benchmark_manifest()
    )
    if manifest_path is not None:
        manifest = load_json(manifest_path)
        if manifest is None:
            print(f"Benchmark manifest is missing or unreadable: {manifest_path}", file=sys.stderr)
            return 2
        scorecard = build_scorecard_from_manifest(manifest, manifest_path=manifest_path)
        scorecard_json, scorecard_md = write_scorecard_outputs(
            scorecard,
            run_dir=manifest_path.parent,
            json_path=Path(args.scorecard_json) if args.scorecard_json else None,
            md_path=Path(args.scorecard_md) if args.scorecard_md else None,
        )
        if args.json:
            print(json.dumps(scorecard, ensure_ascii=False, indent=2))
        else:
            print(
                "Interview scorecard: "
                f"run={scorecard['run']['run_id']}; "
                f"status={scorecard['summary']['status']}; "
                f"json={scorecard_json}; md={scorecard_md}"
            )
        return 0 if scorecard["summary"]["status"] == "passed" else 1

    payload = build_summary(
        live_payload=load_json(Path(args.live_summary)),
        rag_payload=load_json(Path(args.rag_summary)),
        adapter_payload=load_json(Path(args.adapter_summary)),
        milvus_payload=load_json(Path(args.milvus_summary)),
        ragas_payload=load_json(Path(args.ragas_summary)),
        change_payload=load_json(Path(args.change_summary)),
        replanner_payload=load_json(Path(args.replanner_summary)),
        source_artifacts={
            "live_aiops": _artifact_path(args.live_summary),
            "rag": _artifact_path(args.rag_summary),
            "ragas": _artifact_path(args.ragas_summary),
            "adapter_verification": _artifact_path(args.adapter_summary),
            "milvus_multisource": _artifact_path(args.milvus_summary),
            "safe_change": _artifact_path(args.change_summary),
            "replanner": _artifact_path(args.replanner_summary),
        },
    )
    write_outputs(payload, Path(args.summary_json), Path(args.summary_md))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Interview eval summary: "
            f"{payload['summary']['status']}; "
            f"md={args.summary_md}; json={args.summary_json}"
        )
    return 0 if payload["summary"]["interview_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
