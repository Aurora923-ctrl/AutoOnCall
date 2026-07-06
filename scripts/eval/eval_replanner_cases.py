"""Offline Replanner LLM decision evaluation for guardrail and trace behavior."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.replanner import ReplanDecision
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
)
from app.models.incident import Incident
from app.models.plan import PlanStep
from app.tools.base import ToolExecutionResult

replanner_module = importlib.import_module("app.agent.aiops.replanner")

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "replanner_cases.yaml"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "replanner_eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "replanner_eval_summary.md"

REPLANNER_METRIC_NAMES = [
    "decision_hit",
    "decision_source_hit",
    "plan_tool_hit",
    "forbidden_tools_avoided",
    "llm_call_policy_hit",
    "blocked_decision_guardrail",
    "first_step_hit",
    "trace_decision_recorded",
]

REPLANNER_METRIC_FAILURE_REASONS = {
    "decision_hit": "最终 Replanner 决策与 case 期望不一致。",
    "decision_source_hit": "Trace 中记录的 decision_source 与 case 期望不一致。",
    "plan_tool_hit": "Replanner 追加或重试的计划工具未覆盖 case 期望。",
    "forbidden_tools_avoided": "Replanner 计划中出现了 case 禁止的工具。",
    "llm_call_policy_hit": "LLM 调用次数不符合安全优先级约束。",
    "blocked_decision_guardrail": "被禁止的 LLM 决策没有被 guardrail 拦截。",
    "first_step_hit": "Replanner 产出的首个步骤与 case 期望不一致。",
    "trace_decision_recorded": "Trace 没有记录结构化 Replanner 决策来源。",
}


class FakeStructuredLLM:
    """Minimal structured-output LLM stub used by the fake prompt chain."""

    def with_structured_output(self, _schema: Any) -> FakeStructuredLLM:
        return self


class FakeReplannerPrompt:
    """Prompt stub that records whether the LLM path was actually invoked."""

    def __init__(self, decision: ReplanDecision) -> None:
        self.decision = decision
        self.call_count = 0
        self.payloads: list[dict[str, Any]] = []

    def __or__(self, _structured_llm: Any) -> FakeReplannerChain:
        return FakeReplannerChain(self)


class FakeReplannerChain:
    def __init__(self, prompt: FakeReplannerPrompt) -> None:
        self.prompt = prompt

    async def ainvoke(self, payload: dict[str, Any]) -> ReplanDecision:
        self.prompt.call_count += 1
        self.prompt.payloads.append(payload)
        return self.prompt.decision


class RecordingTraceService:
    """In-memory trace service for offline eval assertions."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))

    def record_risk_decision(self, **kwargs: Any) -> None:
        self.events.append({"event_type": "risk_decision", **dict(kwargs)})

    def latest_replan_event(self) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event.get("event_type") == "replan_decision":
                return event
        return None


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load Replanner eval cases from YAML."""
    case_path = Path(path)
    payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No Replanner eval cases found in {case_path}")
    return [dict(case) for case in cases]


async def evaluate_cases(cases_path: str | Path = DEFAULT_CASES_PATH) -> dict[str, Any]:
    """Evaluate all offline Replanner LLM guardrail cases."""
    started_at = datetime.now(UTC)
    started_timer = time.perf_counter()
    cases = load_cases(cases_path)
    results = [await evaluate_case(case) for case in cases]
    ended_at = datetime.now(UTC)
    summary = build_summary(results)
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic Replanner LLM decision regression; LLM and trace "
                "services are in-memory fakes and no production systems are called"
            ),
            "cases_path": str(Path(cases_path)),
            "case_ids": [str(case.get("id", "")) for case in cases],
        },
        "summary": summary,
        "cases": results,
    }


async def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one Replanner case by calling the real replanner function."""
    started = time.perf_counter()
    trace_service = RecordingTraceService()
    fake_prompt = FakeReplannerPrompt(_case_llm_decision(case))

    original_trace_service = replanner_module.trace_service
    original_prompt = replanner_module.replanner_prompt
    original_create_llm = replanner_module._create_llm
    original_enabled = replanner_module.config.aiops_replanner_llm_enabled

    try:
        replanner_module.trace_service = trace_service
        replanner_module.replanner_prompt = fake_prompt
        replanner_module._create_llm = lambda: FakeStructuredLLM()
        replanner_module.config.aiops_replanner_llm_enabled = True

        state = _case_state(case)
        update = await replanner_module.replanner(state)
    finally:
        replanner_module.trace_service = original_trace_service
        replanner_module.replanner_prompt = original_prompt
        replanner_module._create_llm = original_create_llm
        replanner_module.config.aiops_replanner_llm_enabled = original_enabled

    replan_event = trace_service.latest_replan_event() or {}
    metadata = dict(replan_event.get("metadata") or {})
    current_plan = list(update.get("current_plan") or [])
    plan_tools = [str(step.get("tool_name") or "") for step in current_plan if isinstance(step, dict)]
    first_step_id = str(current_plan[0].get("step_id") or "") if current_plan else ""
    actual_decision = str(metadata.get("decision") or "")
    actual_source = str(metadata.get("decision_source") or "")

    metrics = {
        "decision_hit": actual_decision == str(case.get("expected_decision") or ""),
        "decision_source_hit": actual_source == str(case.get("expected_decision_source") or ""),
        "plan_tool_hit": _contains_all(plan_tools, case.get("expected_plan_tools", [])),
        "forbidden_tools_avoided": _has_no_overlap(plan_tools, case.get("forbidden_plan_tools", [])),
        "llm_call_policy_hit": fake_prompt.call_count == int(case.get("expected_llm_call_count", 0)),
        "blocked_decision_guardrail": _blocked_decision_guardrail_hit(case, actual_decision),
        "first_step_hit": _first_step_hit(case, first_step_id),
        "trace_decision_recorded": bool(actual_decision and actual_source),
    }
    failed_metrics = [name for name, passed in metrics.items() if not passed]
    return {
        "id": str(case.get("id") or ""),
        "title": str(case.get("title") or case.get("id") or ""),
        "passed": not failed_metrics,
        "metrics": metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": {
            metric: REPLANNER_METRIC_FAILURE_REASONS[metric] for metric in failed_metrics
        },
        "expected_decision": str(case.get("expected_decision") or ""),
        "actual_decision": actual_decision,
        "expected_decision_source": str(case.get("expected_decision_source") or ""),
        "actual_decision_source": actual_source,
        "expected_plan_tools": [str(item) for item in case.get("expected_plan_tools", [])],
        "actual_plan_tools": plan_tools,
        "forbidden_plan_tools": [str(item) for item in case.get("forbidden_plan_tools", [])],
        "llm_call_count": fake_prompt.call_count,
        "first_step_id": first_step_id,
        "trace_metadata": metadata,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build aggregate Replanner eval metrics."""
    metric_summary = {
        metric: {
            "passed": sum(1 for result in results if result["metrics"].get(metric)),
            "total": len(results),
        }
        for metric in REPLANNER_METRIC_NAMES
    }
    for item in metric_summary.values():
        item["rate"] = _ratio(item["passed"], item["total"])
    passed_count = sum(1 for result in results if result["passed"])
    case_count = len(results)
    return {
        "case_count": case_count,
        "passed_count": passed_count,
        "pass_rate": _ratio(passed_count, case_count),
        "all_passed": passed_count == case_count,
        "metrics": metric_summary,
        "failed_cases": [
            {
                "id": result["id"],
                "failed_metrics": result["failed_metrics"],
                "actual_decision": result["actual_decision"],
                "actual_decision_source": result["actual_decision_source"],
            }
            for result in results
            if not result["passed"]
        ],
        "resume_metrics": {
            "decision_source_hit_rate": _metric_rate(metric_summary, "decision_source_hit"),
            "guardrail_hit_rate": _metric_rate(metric_summary, "blocked_decision_guardrail"),
            "forbidden_tools_avoided_rate": _metric_rate(
                metric_summary,
                "forbidden_tools_avoided",
            ),
            "llm_call_policy_hit_rate": _metric_rate(metric_summary, "llm_call_policy_hit"),
            "trace_decision_recorded_rate": _metric_rate(
                metric_summary,
                "trace_decision_recorded",
            ),
        },
    }


def render_summary(payload: dict[str, Any]) -> str:
    """Render a compact console summary."""
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        (
            f"Replanner eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%})"
        ),
        (
            "Metrics: "
            f"source={resume['decision_source_hit_rate']:.0%}, "
            f"guardrail={resume['guardrail_hit_rate']:.0%}, "
            f"forbidden_avoided={resume['forbidden_tools_avoided_rate']:.0%}, "
            f"trace={resume['trace_decision_recorded_rate']:.0%}"
        ),
    ]
    for result in payload["cases"]:
        suffix = "" if result["passed"] else f" failed={','.join(result['failed_metrics'])}"
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} {result['id']} "
            f"decision={result['actual_decision']} source={result['actual_decision_source']}"
            f"{suffix}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    """Render a reproducible Markdown Replanner eval summary."""
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        "# AutoOnCall Replanner Eval Summary",
        "",
        "## 摘要",
        "",
        f"- Replanner 评测通过率：{summary['passed_count']}/{summary['case_count']} ({summary['pass_rate']:.0%})",
        f"- decision_source 命中率：{resume['decision_source_hit_rate']:.0%}",
        f"- guardrail 命中率：{resume['guardrail_hit_rate']:.0%}",
        f"- forbidden tools avoided：{resume['forbidden_tools_avoided_rate']:.0%}",
        f"- Trace 决策记录率：{resume['trace_decision_recorded_rate']:.0%}",
        f"- 生成时间：{payload['run']['ended_at']}",
        "",
        "## 用例",
        "",
    ]
    for result in payload["cases"]:
        failed = ", ".join(result["failed_metrics"]) if result["failed_metrics"] else "-"
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} `{result['id']}`："
            f"decision={result['actual_decision']}；"
            f"source={result['actual_decision_source']}；failed={failed}"
        )
    return "\n".join(lines) + "\n"


def write_outputs(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
) -> dict[str, str]:
    """Write optional machine-readable and Markdown summaries."""
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


def _case_state(case: dict[str, Any]) -> dict[str, Any]:
    incident = Incident(
        title=str(case.get("title") or case.get("id") or "Replanner eval case"),
        **dict(case.get("incident") or {}),
    )
    state = create_initial_aiops_state(
        str(case.get("input") or incident.symptom),
        session_id=f"replanner-eval-{case.get('id')}",
        incident=incident,
    )
    state["gathered_evidence"] = [
        _evidence_from_case_item(item, incident)
        for item in case.get("evidence", [])
        if isinstance(item, dict)
    ]
    state["tool_call_records"] = [
        _tool_call_record_from_case_item(item, state)
        for item in case.get("tool_call_records", [])
        if isinstance(item, dict)
    ]
    return state


def _evidence_from_case_item(item: dict[str, Any], incident: Incident) -> dict[str, Any]:
    tool_name = str(item.get("tool_name") or "manual_analysis")
    output = dict(item.get("output") or {})
    input_args = dict(item.get("input_args") or {"service_name": incident.service_name})
    result = ToolExecutionResult(
        tool_name=tool_name,
        status=str(item.get("status") or "success"),
        input_args=input_args,
        output=output,
    )
    raw_data = result.model_dump(mode="json")
    summary = str(output.get("summary") or item.get("summary") or "")
    stance = infer_evidence_stance(source_tool=tool_name, raw_data=raw_data, summary=summary)
    return Evidence(
        source_tool=tool_name,
        step_id=str(item.get("step_id") or tool_name),
        summary=summary,
        evidence_type=infer_evidence_type(tool_name),
        stance=stance,
        confidence_reason=build_confidence_reason(
            source_tool=tool_name,
            raw_data=raw_data,
            stance=stance,
        ),
        raw_data=raw_data,
        confidence=float(item.get("confidence", 0.75)),
    ).model_dump(mode="json")


def _tool_call_record_from_case_item(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": state["trace_id"],
        "incident_id": state["incident"]["incident_id"],
        "step_id": str(item.get("step_id") or ""),
        "tool_name": str(item.get("tool_name") or ""),
        "input_args": dict(item.get("input_args") or {}),
        "status": str(item.get("status") or "failed"),
        "error_message": str(item.get("error_message") or ""),
    }


def _case_llm_decision(case: dict[str, Any]) -> ReplanDecision:
    payload = dict(case.get("llm_decision") or {})
    payload["new_steps"] = [
        PlanStep(**dict(step)) for step in payload.get("new_steps", []) if isinstance(step, dict)
    ]
    return ReplanDecision(**payload)


def _blocked_decision_guardrail_hit(case: dict[str, Any], actual_decision: str) -> bool:
    blocked = str(case.get("blocked_decision") or "")
    return actual_decision != blocked if blocked else True


def _first_step_hit(case: dict[str, Any], first_step_id: str) -> bool:
    expected = str(case.get("expected_first_step_id") or "")
    return first_step_id == expected if expected else True


def _contains_all(actual: list[str], expected: list[Any]) -> bool:
    actual_set = set(actual)
    return all(str(item) in actual_set for item in expected)


def _has_no_overlap(actual: list[str], forbidden: list[Any]) -> bool:
    return set(actual).isdisjoint({str(item) for item in forbidden})


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _metric_rate(metric_summary: dict[str, dict[str, Any]], metric: str) -> float:
    item = metric_summary.get(metric, {})
    return float(item.get("rate", 0.0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text summary")
    args = parser.parse_args(argv)

    payload = asyncio.run(evaluate_cases(args.cases))
    write_outputs(
        payload,
        summary_json_path=args.summary_json,
        summary_md_path=args.summary_md,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_summary(payload))
    return 0 if payload["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
