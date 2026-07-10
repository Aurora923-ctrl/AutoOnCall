"""Generate deterministic interview demo diagnosis reports from offline eval cases."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.report import DiagnosisReport  # noqa: E402
from app.services.report_generator import ReportGenerator  # noqa: E402
from scripts.eval.eval_cases import (  # noqa: E402
    DEFAULT_CASES_PATH,
    evaluate_case,
    load_cases,
    load_env_file,
)

DEFAULT_DEMO_CASE_IDS = (
    "redis_maxclients_timeout",
    "mysql_slow_query_latency",
    "pod_crashloop",
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "logs" / "demo_reports"
DEFAULT_REPORT_DB = REPO_ROOT / "logs" / "demo_reports.db"
DEFAULT_SANDBOX_ENV = REPO_ROOT / "deploy" / "sandbox.env"
LIVE_ADAPTER_ENV_KEYS = (
    "PROMETHEUS_BASE_URL",
    "LOKI_BASE_URL",
    "LOG_GATEWAY_URL",
    "REDIS_URL",
    "REDIS_INSTANCES",
    "REDIS_HOST",
    "MYSQL_DSN",
    "MYSQL_URL",
    "MYSQL_INSTANCES",
    "MYSQL_HOST",
)


async def generate_demo_reports(
    *,
    case_ids: list[str] | tuple[str, ...] = DEFAULT_DEMO_CASE_IDS,
    cases_path: str | Path = DEFAULT_CASES_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    report_db_path: str | Path = DEFAULT_REPORT_DB,
    env_file: str | Path | None = DEFAULT_SANDBOX_ENV,
) -> dict[str, Any]:
    """Generate Markdown demo reports and return a machine-readable summary."""
    selected_cases = select_cases(load_cases(cases_path), case_ids)
    if env_file:
        load_env_file(env_file)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    generator = ReportGenerator(report_db_path)

    records: list[dict[str, Any]] = []
    with _offline_eval_fixture_env(enabled=env_file is None):
        for case in selected_cases:
            result = await evaluate_case(case, generator)
            report = find_report(generator, str(result["report_id"]))
            report_path = output / f"{safe_slug(str(case['id']))}.md"
            report_path.write_text(report.markdown, encoding="utf-8")
            records.append(build_record(case, result, report, report_path))

    summary = build_summary(records, cases_path=cases_path, output_dir=output)
    summary["env_file"] = str(Path(env_file)) if env_file else ""
    summary_path = output / "summary.json"
    index_path = output / "index.md"
    summary["artifacts"] = {
        "summary_json": str(summary_path),
        "index_markdown": str(index_path),
        "report_db": str(Path(report_db_path)),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path.write_text(render_index(summary), encoding="utf-8")
    return summary


@contextmanager
def _offline_eval_fixture_env(*, enabled: bool) -> Iterator[None]:
    """Temporarily hide live adapter env so demo reports can use eval fixtures."""
    if not enabled:
        yield
        return
    old_values = {key: os.environ.get(key) for key in LIVE_ADAPTER_ENV_KEYS}
    for key in LIVE_ADAPTER_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def select_cases(
    cases: list[dict[str, Any]],
    case_ids: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return cases in the requested order and fail loudly when one is missing."""
    by_id = {str(case.get("id")): case for case in cases}
    missing = [case_id for case_id in case_ids if case_id not in by_id]
    if missing:
        raise ValueError(f"Demo case id(s) not found in eval cases: {', '.join(missing)}")
    return [by_id[case_id] for case_id in case_ids]


def find_report(generator: ReportGenerator, report_id: str) -> DiagnosisReport:
    """Find one generated report by id from the report store."""
    for report in generator.list_reports():
        if report.report_id == report_id:
            return report
    raise RuntimeError(f"Generated report not found: {report_id}")


def build_record(
    case: dict[str, Any],
    result: dict[str, Any],
    report: DiagnosisReport,
    report_path: Path,
) -> dict[str, Any]:
    """Build the compact per-demo summary used by the index and JSON output."""
    tool_calls = report.tool_calls
    evidence_profile = report.evidence_profile or {}
    root_cause_closure = _as_dict(
        report.evidence_graph.get("root_cause_closure")
        if isinstance(report.evidence_graph, dict)
        else {}
    )
    evidence_sufficiency = report.evidence_sufficiency or _as_dict(
        evidence_profile.get("sufficiency")
    )
    conclusion_alignment = report.conclusion_alignment or {}
    eval_passed = bool(result["passed"])
    demo_passed = eval_passed or demo_report_ready(report)
    return {
        "id": str(case["id"]),
        "title": str(case.get("title") or report.title),
        "service_name": report.service_name,
        "severity": report.severity,
        "environment": report.environment,
        "passed": demo_passed,
        "eval_passed": eval_passed,
        "report_id": report.report_id,
        "report_path": str(report_path),
        "status": report.status,
        "root_cause": report.root_cause,
        "confidence": report.confidence,
        "confidence_reason": report.confidence_reason,
        "evidence_count": len(report.evidence),
        "evidence_profile": evidence_profile,
        "evidence_layers": _as_dict(evidence_profile.get("by_layer")),
        "root_cause_closure": root_cause_closure,
        "evidence_sufficiency": evidence_sufficiency,
        "conclusion_alignment": conclusion_alignment,
        "rca_support_ready": bool(
            root_cause_closure.get("status") == "closed"
            or root_cause_closure.get("status") == "satisfied"
        ),
        "tool_count": len(tool_calls),
        "tools": [str(call.get("tool_name", "unknown")) for call in tool_calls],
        "data_sources": sorted(
            {
                str(call.get("data_source") or "unknown")
                for call in tool_calls
                if call.get("data_source")
            }
        ),
        "failed_metrics": result.get("failed_metrics", []),
        "risk_policy": result.get("risk_policy", "allow"),
        "approval_required": bool(report.manual_action_required),
        "uncertainty_count": len(report.uncertainties),
    }


def demo_report_ready(report: DiagnosisReport) -> bool:
    """Return True when a report is usable for the deterministic interview walkthrough."""
    return bool(
        report.report_id
        and report.markdown
        and report.root_cause
        and report.evidence
        and report.tool_calls
        and report.status in {"completed", "degraded", "waiting_approval", "blocked"}
    )


def build_summary(
    records: list[dict[str, Any]],
    *,
    cases_path: str | Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Build the top-level demo report summary."""
    passed_count = sum(1 for record in records if record["passed"])
    return {
        "scope": (
            "deterministic interview demo reports generated from offline AIOps eval cases; "
            "they validate the diagnostic chain but do not claim production RCA accuracy"
        ),
        "cases_path": str(Path(cases_path)),
        "output_dir": str(output_dir),
        "case_count": len(records),
        "passed_count": passed_count,
        "all_passed": passed_count == len(records),
        "records": records,
    }


def render_index(summary: dict[str, Any]) -> str:
    """Render a Markdown index for quick interview navigation."""
    lines = [
        "# AutoOnCall Demo Reports",
        "",
        (
            f"Generated {summary['case_count']} deterministic demo reports from "
            f"`{summary['cases_path']}`."
        ),
        "",
        "> These reports are offline demo artifacts for interview walkthroughs. "
        "They prove the diagnosis chain is reproducible; "
        "they are not production RCA accuracy claims. A DEMO_PASS with eval_failed_metrics "
        "means the report is usable for walkthrough but a stricter live/eval gate still "
        "needs its own artifact.",
        "",
        "## Mainline",
        "",
        "```text",
        "Alert / Incident -> Planner -> Executor -> Evidence Analyzer -> Replanner",
        "-> Report / Trace / Approval / Eval",
        "```",
        "",
        "## Fixed Demo Cases",
        "",
        "| Case | Demo result | Eval result | Service | RCA closure | Evidence layers | Confidence | Risk | Report |",
        "| --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for record in summary["records"]:
        result = "DEMO_PASS" if record["passed"] else "CHECK"
        eval_result = "EVAL_PASS" if record.get("eval_passed", record["passed"]) else "EVAL_CHECK"
        report_name = Path(record["report_path"]).name
        closure = _as_dict(record.get("root_cause_closure"))
        lines.append(
            "| "
            f"{record['id']} | "
            f"{result} | "
            f"{eval_result} | "
            f"{record['service_name']} | "
            f"{closure.get('status') or 'unknown'} | "
            f"{_render_counter(_as_dict(record.get('evidence_layers')))} | "
            f"{record['confidence']:.2f} | "
            f"{record['risk_policy']} | "
            f"[{report_name}]({report_name}) |"
        )

    lines.extend(
        [
            "",
            "## What To Show In 10 Minutes",
            "",
            "1. Open one report and point at the tool-call table.",
            "2. Show the evidence matrix and explain supporting/refuting/unknown evidence.",
            "3. Show confidence and uncertainty instead of claiming perfect RCA.",
            "4. Show risk policy to explain why high-risk actions are bounded.",
            "5. Open eval summary to prove this is a repeatable regression check.",
            "6. For negative-boundary questions, show `needs_human` or `degraded` instead of a forced completed RCA.",
            "",
            "## RCA Evidence Closure",
            "",
            "| Case | Report status | RCA closure | Live evidence | Knowledge/history | Missing | Conclusion alignment |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for record in summary["records"]:
        closure = _as_dict(record.get("root_cause_closure"))
        alignment = _as_dict(record.get("conclusion_alignment"))
        lines.append(
            "| "
            f"{record['id']} | "
            f"{record['status']} | "
            f"{closure.get('status') or 'unknown'} | "
            f"{_render_inline_list(closure.get('live_evidence_ids'))} | "
            f"{_render_inline_list(_reference_ids(closure))} | "
            f"{_render_inline_list(closure.get('missing') or closure.get('missing_layers'))} | "
            f"{alignment.get('status') or 'unknown'} |"
        )

    lines.extend(
        [
            "",
            "## Evidence Layer Summary",
            "",
            "| Case | Layers | Sufficiency | Missing evidence | Failed tools |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for record in summary["records"]:
        sufficiency = _as_dict(record.get("evidence_sufficiency"))
        lines.append(
            "| "
            f"{record['id']} | "
            f"{_render_counter(_as_dict(record.get('evidence_layers')))} | "
            f"{sufficiency.get('status') or 'unknown'} | "
            f"{_render_inline_list(sufficiency.get('missing_evidence'))} | "
            f"{_render_inline_list(sufficiency.get('failed_tools'))} |"
        )

    lines.extend(
        [
            "",
            "## Demo Talking Points",
            "",
        ]
    )
    for record in summary["records"]:
        lines.extend(
            [
                f"### {record['id']}",
                f"- Root cause: {record['root_cause']}",
                f"- Confidence: {record['confidence']:.2f}; reason: {record['confidence_reason']}",
                f"- Risk policy: {record['risk_policy']}; status: {record['status']}",
                f"- Tools: {', '.join(record['tools']) or 'none'}",
                f"- Data sources: {', '.join(record['data_sources']) or 'unknown'}",
                f"- Eval failed metrics: {', '.join(record.get('failed_metrics', [])) or 'none'}",
                f"- Evidence profile: {_render_profile(record['evidence_profile'])}",
                f"- RCA closure: {_render_closure(record.get('root_cause_closure'))}",
                f"- Conclusion alignment: {_as_dict(record.get('conclusion_alignment')).get('status') or 'unknown'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Boundary Statement",
            "",
            "- These reports are generated from deterministic offline eval cases.",
            "- `mock`, `eval_fixture`, and `not_configured` sources are demo/eval boundaries.",
            "- Adapter-backed sandbox runs should show sources such as `prometheus`, `loki`, "
            "`redis_info`, `mysql`, or `kubernetes`.",
            "- Passing eval means the core chain has not regressed; it is not a production "
            "accuracy claim.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_profile(profile: dict[str, Any]) -> str:
    """Render a compact evidence profile for the demo index."""
    if not profile:
        return "unknown"
    parts: list[str] = []
    for key in ("by_stance", "by_type", "by_data_source"):
        value = profile.get(key)
        if isinstance(value, dict) and value:
            rendered = ", ".join(f"{name}={count}" for name, count in sorted(value.items()))
            parts.append(f"{key}: {rendered}")
    return "; ".join(parts) if parts else "unknown"


def _render_closure(value: Any) -> str:
    closure = _as_dict(value)
    if not closure:
        return "unknown"
    return (
        f"{closure.get('status') or 'unknown'}; "
        f"live={_render_inline_list(closure.get('live_evidence_ids'))}; "
        f"knowledge/history={_render_inline_list(_reference_ids(closure))}; "
        f"missing={_render_inline_list(closure.get('missing') or closure.get('missing_layers'))}"
    )


def _reference_ids(closure: dict[str, Any]) -> list[Any]:
    return [
        *list(closure.get("knowledge_evidence_ids") or []),
        *list(closure.get("history_evidence_ids") or []),
    ]


def _render_counter(value: dict[str, Any]) -> str:
    if not value:
        return "unknown"
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def _render_inline_list(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def safe_slug(value: str) -> str:
    """Return a filesystem-safe slug for generated report names."""
    basename = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", basename.strip()).strip(".-")
    return slug or "demo-report"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--report-db", default=str(DEFAULT_REPORT_DB))
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_SANDBOX_ENV),
        help="Optional env file for live adapter-backed demo generation.",
    )
    parser.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Use deterministic offline fixtures instead of loading the sandbox env file.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Demo case id to include. Repeat to control order.",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON summary")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    case_ids = args.case_ids or list(DEFAULT_DEMO_CASE_IDS)
    summary = asyncio.run(
        generate_demo_reports(
            case_ids=case_ids,
            cases_path=args.cases,
            output_dir=args.output_dir,
            report_db_path=args.report_db,
            env_file=None if args.offline_fixtures else args.env_file,
        )
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"Demo reports: {summary['passed_count']}/{summary['case_count']} passed; "
            f"index={summary['artifacts']['index_markdown']}"
        )
        for record in summary["records"]:
            status = "PASS" if record["passed"] else "FAIL"
            print(f"- {status} {record['id']} -> {record['report_path']}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
