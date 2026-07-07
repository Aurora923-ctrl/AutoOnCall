"""Build a single interview-facing summary from eval artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LIVE_SUMMARY = REPO_ROOT / "logs" / "live_golden_eval_summary_current.json"
DEFAULT_RAG_SUMMARY = REPO_ROOT / "logs" / "rag_eval_summary_current.json"
DEFAULT_ADAPTER_SUMMARY = REPO_ROOT / "logs" / "full_stack_adapter_verification.json"
DEFAULT_CHANGE_SUMMARY = REPO_ROOT / "logs" / "change_eval_summary.json"
DEFAULT_REPLANNER_SUMMARY = REPO_ROOT / "logs" / "replanner_eval_summary.json"
DEFAULT_OUTPUT_JSON = REPO_ROOT / "logs" / "interview_eval_summary.json"
DEFAULT_OUTPUT_MD = REPO_ROOT / "logs" / "interview_eval_summary.md"

CORE_CASE_IDS = ["redis_maxclients_timeout", "mysql_slow_query_latency", "pod_crashloop"]
CORE_MODULES = ["adapter_verification", "live_aiops_eval", "rag_eval"]


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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
        }

    summary = payload.get("summary", {})
    status = payload.get("status")
    if status in {"passed", "failed"}:
        passed = status == "passed"
        return {
            "name": name,
            "status": status,
            "passed": passed,
            "case_count": len(payload.get("checks", [])),
            "passed_count": sum(1 for check in payload.get("checks", []) if check.get("passed")),
            "pass_rate": 1.0 if passed else 0.0,
        }

    case_count = int(summary.get("overall_case_count") or summary.get("case_count") or 0)
    passed_count = int(summary.get("overall_passed_count") or summary.get("passed_count") or 0)
    all_passed = bool(summary.get("all_passed", passed_count == case_count and case_count > 0))
    pass_rate = float(summary.get("overall_pass_rate") or summary.get("pass_rate") or 0.0)
    return {
        "name": name,
        "status": "passed" if all_passed else "failed",
        "passed": all_passed,
        "case_count": case_count,
        "passed_count": passed_count,
        "pass_rate": pass_rate,
    }


def build_summary(
    *,
    live_payload: dict[str, Any] | None,
    rag_payload: dict[str, Any] | None,
    adapter_payload: dict[str, Any] | None,
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

    return {
        "run": {
            "generated_at": datetime.now(UTC).isoformat(),
            "summary_scope": (
                "interview-facing rollup; live AIOps and RAG evals remain separate "
                "source artifacts to avoid treating --skip-rag as a RAG result"
            ),
            "source_artifacts": {
                "live_aiops": str(DEFAULT_LIVE_SUMMARY.relative_to(REPO_ROOT)),
                "rag": str(DEFAULT_RAG_SUMMARY.relative_to(REPO_ROOT)),
                "adapter_verification": str(DEFAULT_ADAPTER_SUMMARY.relative_to(REPO_ROOT)),
                "safe_change": str(DEFAULT_CHANGE_SUMMARY.relative_to(REPO_ROOT)),
                "replanner": str(DEFAULT_REPLANNER_SUMMARY.relative_to(REPO_ROOT)),
            }
            if source_artifacts is None
            else source_artifacts,
        },
        "summary": {
            "status": "passed" if core_passed else "failed",
            "core_modules_passed": core_passed,
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
            "rag_metrics": _rag_metrics(rag_payload),
            "adapter_sources": {
                "status": (adapter_payload or {}).get("status", "missing"),
                "data_sources": (adapter_payload or {}).get("data_sources", []),
                "mock_fallback_detected": (adapter_payload or {}).get(
                    "mock_fallback_detected"
                ),
                "missing_sources": (adapter_payload or {}).get("missing_sources", []),
                "failed_tools": (adapter_payload or {}).get("failed_tools", []),
            },
            "interview_boundaries": [
                "Redis/MySQL are live adapter golden chains backed by the local Docker stack.",
                "RAG eval is shown from its own retrieval summary, not from the --skip-rag AIOps run.",
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
        "runtime_vs_incident_boundary_hit": bool(
            metrics.get("runtime_vs_incident_boundary_hit")
        ),
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
        "no_answer_rejection_rate": float(
            summary.get("no_answer_rejection_rate", 0.0) or 0.0
        ),
        "confusion_case_pass_rate": float(
            summary.get("confusion_case_pass_rate", 0.0) or 0.0
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    modules = summary["modules"]
    chains = summary["portfolio_chains"]
    rag = summary["rag_metrics"]
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
            f"- citation coverage: `{_ratio_percent(rag['citation_coverage_rate'])}`",
            f"- no-answer rejection: `{_ratio_percent(rag['no_answer_rejection_rate'])}`",
            f"- confusion case pass: `{_ratio_percent(rag['confusion_case_pass_rate'])}`",
            "",
            "## Adapter Snapshot",
            "",
            f"- Adapter verification: `{adapter['status']}`",
            f"- Data sources: `{', '.join(adapter['data_sources']) or 'missing'}`",
            f"- mock_fallback_detected: `{adapter['mock_fallback_detected']}`",
            f"- missing_sources: `{adapter['missing_sources']}`",
            f"- failed_tools: `{adapter['failed_tools']}`",
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-summary", default=str(DEFAULT_LIVE_SUMMARY))
    parser.add_argument("--rag-summary", default=str(DEFAULT_RAG_SUMMARY))
    parser.add_argument("--adapter-summary", default=str(DEFAULT_ADAPTER_SUMMARY))
    parser.add_argument("--change-summary", default=str(DEFAULT_CHANGE_SUMMARY))
    parser.add_argument("--replanner-summary", default=str(DEFAULT_REPLANNER_SUMMARY))
    parser.add_argument("--summary-json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_OUTPUT_MD))
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_summary(
        live_payload=load_json(Path(args.live_summary)),
        rag_payload=load_json(Path(args.rag_summary)),
        adapter_payload=load_json(Path(args.adapter_summary)),
        change_payload=load_json(Path(args.change_summary)),
        replanner_payload=load_json(Path(args.replanner_summary)),
        source_artifacts={
            "live_aiops": args.live_summary,
            "rag": args.rag_summary,
            "adapter_verification": args.adapter_summary,
            "safe_change": args.change_summary,
            "replanner": args.replanner_summary,
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
    return 0 if payload["summary"]["core_modules_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
