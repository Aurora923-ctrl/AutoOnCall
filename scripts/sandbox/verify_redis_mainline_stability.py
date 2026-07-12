"""Run the Redis interview mainline repeatedly and verify stable business conclusions."""

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
DEFAULT_OUTPUT_PATH = ROOT / "logs" / "redis_mainline_stability.json"

EXPECTED_TOOL_ORDER = [
    "query_redis_status",
    "query_metrics",
    "query_logs",
    "search_runbook",
    "search_history_ticket",
    "suggest_remediation",
    "apply_config_change",
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

    incident_model = build_demo_incident("redis_maxclients")
    incident = incident_model.model_dump(mode="json")
    input_text = f"{incident['title']} {incident['symptom']}"
    plan = build_fallback_plan(input_text, incident)
    registry = create_default_tool_registry([]).with_incident_context(incident)

    evidence: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    risk_decisions: list[dict[str, Any]] = []
    executed_tools: list[str] = []

    for step in plan:
        decision = assess_plan_step(step, tool_registry=registry, incident=incident)
        risk_decisions.append(decision.model_dump(mode="json"))
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
                    "trace_id": f"trace-redis-stability-{run_index}",
                    "incident": incident,
                },
            ).model_dump(mode="json")
        )
        executed_tools.append(step.tool_name)

    diagnostic_evidence = [
        item for item in evidence if item.get("source_tool") != "suggest_remediation"
    ]
    diagnostic_tool_calls = [
        item for item in tool_calls if item.get("tool_name") != "suggest_remediation"
    ]
    analysis_state = {
        "input": input_text,
        "incident": incident,
        "trace_id": f"trace-redis-stability-{run_index}",
        "gathered_evidence": diagnostic_evidence,
        "tool_call_records": diagnostic_tool_calls,
        "current_plan": [],
        "plan": [],
    }
    analysis = analyze_evidence(analysis_state)
    state = {
        **analysis_state,
        "gathered_evidence": evidence,
        "tool_call_records": tool_calls,
    }
    state["hypotheses"] = analysis.hypotheses
    state["evidence_analysis"] = analysis.model_dump(mode="json")

    report_path = ROOT / "logs" / f"redis-mainline-stability-{run_index}.db"
    report_path.unlink(missing_ok=True)
    report = ReportGenerator(report_path).generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )
    report_path.unlink(missing_ok=True)

    tool_order = [step.tool_name for step in plan]
    approval_decisions = [
        decision for decision in risk_decisions if decision["policy"] == "approval_required"
    ]
    redis_call = next(
        (call for call in tool_calls if call["tool_name"] == "query_redis_status"),
        {},
    )
    redis_output = redis_call.get("output") if isinstance(redis_call.get("output"), dict) else {}
    return {
        "run": run_index,
        "passed": (
            tool_order == EXPECTED_TOOL_ORDER
            and executed_tools == EXPECTED_TOOL_ORDER[:-1]
            and analysis.decision == "generate_report"
            and analysis.evidence_sufficient
            and bool(analysis.hypothesis_ranking)
            and analysis.hypothesis_ranking[0].category == "redis_maxclients"
            and "Redis" in report.root_cause
            and "maxclients" in report.root_cause
            and report.status in {"completed", "needs_human"}
            and len(approval_decisions) == 1
            and approval_decisions[0]["tool_name"] == "apply_config_change"
            and redis_output.get("connected_clients") == 9940
            and redis_output.get("maxclients") == 10000
            and redis_output.get("live_connected_clients", 0) < 100
        ),
        "tool_order": tool_order,
        "executed_tools": executed_tools,
        "sources": [call["data_source"] for call in tool_calls],
        "statuses": [call["status"] for call in tool_calls],
        "root_cause_category": (
            analysis.hypothesis_ranking[0].category if analysis.hypothesis_ranking else ""
        ),
        "root_cause": report.root_cause,
        "report_status": report.status,
        "risk_policy": approval_decisions[0]["policy"] if approval_decisions else "allow",
        "approval_tool": approval_decisions[0]["tool_name"] if approval_decisions else "",
        "incident_window": {
            "connected_clients": redis_output.get("connected_clients"),
            "maxclients": redis_output.get("maxclients"),
        },
        "current_runtime": {
            "connected_clients": redis_output.get("live_connected_clients"),
            "maxclients": redis_output.get("live_maxclients"),
        },
    }


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
                "risk_policy": item["risk_policy"],
                "approval_tool": item["approval_tool"],
                "incident_window": item["incident_window"],
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
    parser = argparse.ArgumentParser(description="Verify Redis mainline stability.")
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
            f"Redis mainline stability: {payload['status'].upper()} "
            f"({payload['passed_run_count']}/{payload['run_count']})"
        )
        print(f"Stable business signature: {payload['stable_business_signature']}")
        print(f"Report: {output_path}")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
