"""Run the MySQL interview mainline repeatedly and verify its distinct diagnosis."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ENV = ROOT / "deploy" / "sandbox.env"
DEFAULT_OUTPUT_PATH = ROOT / "logs" / "mysql_mainline_stability.json"

EXPECTED_TOOL_ORDER = [
    "query_mysql_status",
    "query_metrics",
    "query_logs",
    "query_deploy_history",
    "search_runbook",
    "search_history_ticket",
    "suggest_remediation",
]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


async def run_once(run_index: int) -> dict[str, Any]:
    from app.agent.aiops.evidence_analyzer import analyze_evidence
    from app.agent.aiops.plan_fallback import build_fallback_plan
    from app.agent.aiops.risk_controller import assess_plan_step
    from app.services.aiops_execution_records import (
        result_for_persistence,
        tool_result_to_call_record,
        tool_result_to_evidence,
    )
    from app.services.demo_incidents import build_demo_incident
    from app.services.report_generator import ReportGenerator
    from app.tools.registry import create_default_tool_registry

    incident_model = build_demo_incident("mysql_slow_query")
    incident = incident_model.model_dump(mode="json")
    input_text = f"{incident['title']} {incident['symptom']}"
    plan = build_fallback_plan(input_text, incident)
    registry = create_default_tool_registry([]).with_incident_context(incident)

    evidence: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    executed_tools: list[str] = []
    risk_policies: list[str] = []

    for step in plan:
        decision = assess_plan_step(step, tool_registry=registry, incident=incident)
        risk_policies.append(decision.policy)
        if decision.policy != "allow":
            continue
        result = result_for_persistence(
            await registry.arun(
                step.tool_name,
                dict(step.input_args),
                incident=incident,
                step=step,
            )
        )
        evidence.append(tool_result_to_evidence(result, step).model_dump(mode="json"))
        tool_calls.append(
            tool_result_to_call_record(
                result,
                step,
                {
                    "trace_id": f"trace-mysql-stability-{run_index}",
                    "incident": incident,
                },
            ).model_dump(mode="json")
        )
        executed_tools.append(step.tool_name)

    analysis_state = {
        "input": input_text,
        "incident": incident,
        "trace_id": f"trace-mysql-stability-{run_index}",
        "gathered_evidence": evidence,
        "tool_call_records": tool_calls,
        "current_plan": [],
        "plan": [],
    }
    analysis = analyze_evidence(analysis_state)
    state = {
        **analysis_state,
        "hypotheses": analysis.hypotheses,
        "evidence_analysis": analysis.model_dump(mode="json"),
    }

    report_path = ROOT / "logs" / f"mysql-mainline-stability-{run_index}.db"
    report_path.unlink(missing_ok=True)
    report = ReportGenerator(report_path).generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )
    report_path.unlink(missing_ok=True)

    calls_by_tool = {call["tool_name"]: call for call in tool_calls}
    mysql_output = _output(calls_by_tool.get("query_mysql_status"))
    metrics_output = _output(calls_by_tool.get("query_metrics"))
    logs_output = _output(calls_by_tool.get("query_logs"))
    deploy_output = _output(calls_by_tool.get("query_deploy_history"))
    report_text = f"{report.root_cause} {report.markdown}".lower()
    return {
        "run": run_index,
        "passed": (
            [step.tool_name for step in plan] == EXPECTED_TOOL_ORDER
            and executed_tools == EXPECTED_TOOL_ORDER
            and set(risk_policies) == {"allow"}
            and analysis.decision == "generate_report"
            and analysis.evidence_profile.get("sufficiency", {}).get("complete") is True
            and analysis.evidence_profile.get("root_cause_closure_status") == "satisfied"
            and bool(analysis.hypothesis_ranking)
            and analysis.hypothesis_ranking[0].category == "mysql_slow_query"
            and mysql_output.get("signals", {}).get("pool_waiting", 0) >= 1
            and mysql_output.get("signals", {}).get("active_connections", 0) >= 180
            and metrics_output.get("signals", {}).get("p95_latency_ms", 0) >= 2000
            and metrics_output.get("signals", {}).get("error_rate", 1) <= 0.02
            and metrics_output.get("signals", {}).get("cpu_usage_percent", 0) >= 70
            and logs_output.get("slow_sql_digest") == "9f3a-pay-report"
            and deploy_output.get("signals", {}).get("feature_flag_change") is True
            and "slow" in report_text
            and "pool" in report_text
            and "approval" in report_text
            and "cpu" in report_text
            and "cannot" in report_text
        ),
        "tool_order": [step.tool_name for step in plan],
        "executed_tools": executed_tools,
        "sources": [call["data_source"] for call in tool_calls],
        "statuses": [call["status"] for call in tool_calls],
        "root_cause_category": (
            analysis.hypothesis_ranking[0].category if analysis.hypothesis_ranking else ""
        ),
        "root_cause": report.root_cause,
        "report_status": report.status,
        "mysql": mysql_output.get("signals", {}),
        "metrics": metrics_output.get("signals", {}),
        "slow_sql_digest": logs_output.get("slow_sql_digest"),
        "release_correlation": deploy_output.get("release_correlation", {}),
    }


def _output(call: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    output = call.get("output")
    return output if isinstance(output, dict) else {}


async def verify_stability(runs: int) -> dict[str, Any]:
    results = [await run_once(index) for index in range(1, runs + 1)]
    signatures = {
        json.dumps(
            {
                "tool_order": item["tool_order"],
                "sources": item["sources"],
                "statuses": item["statuses"],
                "root_cause_category": item["root_cause_category"],
                "report_status": item["report_status"],
                "mysql": item["mysql"],
                "metrics": item["metrics"],
                "slow_sql_digest": item["slow_sql_digest"],
                "release_correlation": item["release_correlation"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in results
    }
    passed = all(item["passed"] for item in results) and len(signatures) == 1
    return {
        "status": "passed" if passed else "failed",
        "run_count": runs,
        "passed_run_count": sum(1 for item in results if item["passed"]),
        "stable_business_signature": len(signatures) == 1,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify MySQL mainline stability.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--env-file", default=str(SANDBOX_ENV))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))
    os.environ.setdefault("AIOPS_MOCK_FALLBACK_ENABLED", "false")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    payload = asyncio.run(verify_stability(max(args.runs, 1)))
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"MySQL mainline stability: {payload['status'].upper()} "
            f"({payload['passed_run_count']}/{payload['run_count']})"
        )
        print(f"Stable business signature: {payload['stable_business_signature']}")
        print(f"Report: {output_path}")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
