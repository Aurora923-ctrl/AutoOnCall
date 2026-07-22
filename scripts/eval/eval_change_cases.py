"""Deterministic evaluation for AIOps safety policy and safe-change boundaries."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent.aiops.risk_controller import assess_plan_step
from app.models.approval import ApprovalRequest
from app.models.change_execution import ManualExecutionResultRequest
from app.models.incident import utc_now
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.services.approval_service import ApprovalService
from app.services.approval_workflow import create_approval_request_from_risk_decision
from app.services.change_execution_service import ChangeExecutionService
from app.services.change_plan_builder import build_change_plan
from app.services.policies.approval_policy import RISK_POLICY_VERSION
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService
from scripts.eval.eval_environment import (
    collect_dataset_provenance,
    collect_eval_environment,
    provenance_markdown_lines,
)

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "change_cases.yaml"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "change_eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "change_eval_summary.md"

POLICIES = ("forbidden", "approval_required", "allow")
CASE_METRICS = (
    "policy_correct",
    "safe_not_false_blocked",
    "approval_not_bypassed",
    "unauthorized_execution_prevented",
    "prompt_injection_resisted",
    "argument_injection_resisted",
    "sensitive_data_redacted",
    "change_plan_complete",
    "dry_run_before_execute",
    "rollback_recommended",
    "concurrent_approval_consistent",
)


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load and validate safety evaluation cases."""
    case_path = Path(path)
    payload = _load_yaml_payload(case_path)
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No change eval cases found in {case_path}")
    normalized = [dict(case) for case in cases]
    ids = [str(case.get("id") or "") for case in normalized]
    if any(not case_id for case_id in ids):
        raise ValueError("Every change eval case requires a non-empty id")
    if len(ids) != len(set(ids)):
        raise ValueError("Change eval case ids must be unique")
    if len(normalized) < 40:
        raise ValueError("Safety evaluation requires at least 40 positive and negative cases")
    supported = {"policy", "safe_change", "sensitive_redaction", "concurrent_approval"}
    unknown = sorted({str(case.get("scenario") or "") for case in normalized} - supported)
    if unknown:
        raise ValueError(f"Unsupported change eval scenarios: {unknown}")
    return normalized


async def evaluate_cases(cases_path: str | Path = DEFAULT_CASES_PATH) -> dict[str, Any]:
    """Evaluate all cases against isolated deterministic services."""
    started_at = datetime.now(UTC)
    evaluation_run_id = f"eval-change-{uuid4().hex}"
    started_timer = time.perf_counter()
    cases = load_cases(cases_path)
    with TemporaryDirectory(prefix="autooncall-change-eval-") as temp_dir:
        runtime = _build_runtime(Path(temp_dir) / "change_eval.db")
        results = [await evaluate_case(case, runtime) for case in cases]

    ended_at = datetime.now(UTC)
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic safety regression using the production risk controller, "
                "approval service, safe-change service, and trace redaction; no production write "
                "adapter is used"
            ),
            "cases_path": str(Path(cases_path)),
            "dataset": collect_dataset_provenance(cases_path, case_count=len(cases)),
            "case_ids": [str(case["id"]) for case in cases],
            "environment": collect_eval_environment(
                suite="safe_change",
                run_id=evaluation_run_id,
                execution_identity={
                    "actual_model": "deterministic-safe-change-fixture",
                    "actual_embedding_model": "not_used",
                    "provider": "local",
                    "execution_path": "deterministic_fixture",
                    "fallback_used": False,
                    "model_calls": [],
                },
            ),
            "run_id": evaluation_run_id,
        },
        "summary": build_summary(results),
        "cases": results,
    }


async def evaluate_case(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one safety case and retain its prediction evidence."""
    started = time.perf_counter()
    scenario = str(case["scenario"])
    if scenario == "policy":
        result = _evaluate_policy_case(case, runtime)
    elif scenario == "safe_change":
        result = await _evaluate_safe_change_case(case, runtime)
    elif scenario == "sensitive_redaction":
        result = _evaluate_sensitive_redaction_case(case, runtime)
    else:
        result = await _evaluate_concurrent_approval_case(case, runtime)
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result["failed_metrics"] = [
        name
        for name, applicable in result["metric_applicability"].items()
        if applicable and not result["metrics"][name]
    ]
    result["passed"] = not result["failed_metrics"]
    return result


def _evaluate_policy_case(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    incident_id = str(case.get("incident_id") or f"inc-{case['id']}")
    input_args = dict(case.get("input_args") or {})
    decision = assess_plan_step(
        PlanStep(
            tool_name=str(case.get("tool_name") or "query_metrics"),
            purpose=str(case.get("action") or case["id"]),
            input_args=input_args,
            expected_evidence=str(case.get("expected_evidence") or "safety evaluation"),
            risk_level=str(case.get("risk_level") or "low"),
        ),
        incident={
            "incident_id": incident_id,
            "environment": str(case.get("environment") or "prod"),
            "title": str(case.get("incident_title") or ""),
        },
    )
    expected_policy = str(case["expected_policy"])
    stored_approvals = runtime["approval_service"].list_requests(incident_id=incident_id)
    stored_executions = runtime["change_service"].list_executions(incident_id=incident_id)
    tags = _string_list(case.get("tags"))
    policy_correct = decision.policy == expected_policy
    metrics, applicability = _blank_metrics()
    metrics["policy_correct"] = policy_correct
    applicability["policy_correct"] = True
    if expected_policy == "allow":
        metrics["safe_not_false_blocked"] = decision.policy == "allow"
        applicability["safe_not_false_blocked"] = True
    if expected_policy in {"approval_required", "forbidden"}:
        metrics["approval_not_bypassed"] = not stored_executions
        metrics["unauthorized_execution_prevented"] = not stored_approvals and not stored_executions
        applicability["approval_not_bypassed"] = True
        applicability["unauthorized_execution_prevented"] = True
    if "prompt_injection" in tags:
        metrics["prompt_injection_resisted"] = policy_correct
        applicability["prompt_injection_resisted"] = True
    if "argument_injection" in tags:
        metrics["argument_injection_resisted"] = policy_correct
        applicability["argument_injection_resisted"] = True
    return _case_result(
        case,
        actual_status=decision.policy,
        expected_status=expected_policy,
        metrics=metrics,
        applicability=applicability,
        evidence={
            "risk_level": decision.risk_level,
            "allowed": decision.allowed,
            "forbidden": decision.forbidden,
            "need_approval": decision.need_approval,
            "matched_rules": decision.matched_rules,
            "stored_approval_count": len(stored_approvals),
            "stored_execution_count": len(stored_executions),
        },
    )


async def _evaluate_safe_change_case(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    approval_service: ApprovalService = runtime["approval_service"]
    change_service: ChangeExecutionService = runtime["change_service"]
    approval, plan = _create_approval(case, approval_service)
    requested_incident_id = str(case.get("requested_incident_id") or approval.incident_id)
    requested_plan_id = str(case.get("requested_plan_id") or plan.change_plan_id)
    requested_approval_id = str(case.get("requested_approval_id") or approval.approval_id)
    events: list[dict[str, Any]] = []
    error = ""
    try:
        events = [
            event
            async for event in change_service.start_after_approval(
                incident_id=requested_incident_id,
                change_plan_id=requested_plan_id,
                approval_id=requested_approval_id,
                mode=str(case.get("mode") or "dry_run_only"),
                operator="change-eval",
            )
        ]
    except Exception as exc:
        error = str(exc)

    executions = change_service.list_executions(change_plan_id=plan.change_plan_id)
    execution = executions[-1] if executions else None
    manual_result = case.get("manual_result")
    if execution is not None and manual_result and execution.status == "waiting_manual_execution":
        execution = change_service.record_manual_result(
            execution.change_execution_id,
            ManualExecutionResultRequest(
                status=str(manual_result.get("status") or "failed"),  # type: ignore[arg-type]
                operator="change-eval-executor",
                notes=str(manual_result.get("notes") or ""),
                observed_metrics=dict(manual_result.get("observed_metrics") or {}),
            ),
        )

    actual_status = execution.status if execution is not None else _status_without_execution(error)
    expected_status = str(case["expected_status"])
    event_types = [str(event.get("type") or "") for event in events]
    plan_payload = plan.model_dump(mode="json")
    execution_payload = execution.model_dump(mode="json") if execution is not None else {}
    dry_run = dict(execution_payload.get("dry_run") or {})
    pre_check = dict(execution_payload.get("pre_check") or {})
    execution_index = _first_index(event_types, "change_execution")
    dry_run_index = _first_index(event_types, "change_dry_run")
    dry_run_before_execute = execution_index == -1 or (
        dry_run_index != -1 and dry_run_index < execution_index
    )
    if dry_run.get("status") == "failed":
        dry_run_before_execute = dry_run_before_execute and execution_index == -1

    blocked_expected = expected_status == "rejected_before_execution"
    status_ok = (
        execution is None and bool(error) if blocked_expected else actual_status == expected_status
    )
    metrics, applicability = _blank_metrics()
    metrics["policy_correct"] = status_ok
    metrics["change_plan_complete"] = bool(
        plan_payload.get("pre_checklist")
        and (plan_payload.get("execution_steps") or plan_payload.get("steps"))
        and (plan_payload.get("rollback_steps") or plan_payload.get("rollback_plan"))
        and plan_payload.get("observe_metrics")
    )
    metrics["dry_run_before_execute"] = bool(dry_run_before_execute)
    applicability["policy_correct"] = True
    applicability["change_plan_complete"] = True
    applicability["dry_run_before_execute"] = True

    if blocked_expected:
        metrics["approval_not_bypassed"] = execution is None
        metrics["unauthorized_execution_prevented"] = execution is None
        applicability["approval_not_bypassed"] = True
        applicability["unauthorized_execution_prevented"] = True
    if expected_status == "rollback_recommended":
        metrics["rollback_recommended"] = actual_status == "rollback_recommended" and bool(
            execution_payload.get("rollback_result")
        )
        applicability["rollback_recommended"] = True
    expected_precheck = str(case.get("expected_precheck") or "")
    expected_dry_run = str(case.get("expected_dry_run") or "")
    if expected_precheck:
        metrics["policy_correct"] = (
            metrics["policy_correct"] and pre_check.get("status") == expected_precheck
        )
    if expected_dry_run:
        metrics["dry_run_before_execute"] = (
            metrics["dry_run_before_execute"] and dry_run.get("status") == expected_dry_run
        )
    return _case_result(
        case,
        actual_status=actual_status,
        expected_status=expected_status,
        metrics=metrics,
        applicability=applicability,
        error=error,
        evidence={
            "approval_status": approval.status,
            "event_types": event_types,
            "execution_count": len(executions),
            "pre_check_status": pre_check.get("status"),
            "dry_run_status": dry_run.get("status"),
            "rollback_result": execution_payload.get("rollback_result") or {},
        },
    )


def _evaluate_sensitive_redaction_case(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    trace_service: TraceService = runtime["trace_service"]
    secrets = _string_list(case.get("secrets"))
    event = trace_service.record_tool_call(
        ToolCallRecord(
            trace_id=f"trace-{case['id']}",
            incident_id=f"inc-{case['id']}",
            step_id="security-redaction",
            tool_name="query_logs",
            input_args=dict(case.get("input_args") or {}),
            output=case.get("output"),
            output_summary=str(case.get("output_summary") or ""),
            error_message=str(case.get("error_message") or "") or None,
            status="success",
        )
    )
    persisted = trace_service.list_events(trace_id=f"trace-{case['id']}")[0]
    serialized = json.dumps(
        [event.model_dump(mode="json"), persisted.model_dump(mode="json")],
        ensure_ascii=False,
        default=str,
    )
    leaked = [secret for secret in secrets if secret and secret in serialized]
    metrics, applicability = _blank_metrics()
    metrics["sensitive_data_redacted"] = not leaked
    applicability["sensitive_data_redacted"] = True
    return _case_result(
        case,
        actual_status="redacted" if not leaked else "leaked",
        expected_status="redacted",
        metrics=metrics,
        applicability=applicability,
        evidence={"leaked_markers": leaked, "persisted_event_id": persisted.event_id},
    )


async def _evaluate_concurrent_approval_case(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    approval_service: ApprovalService = runtime["approval_service"]
    approval, _ = _create_approval(
        {**case, "approval_status": "pending", "action": "restart service"},
        approval_service,
    )

    async def decide(decision: str) -> dict[str, str]:
        try:
            result = await asyncio.to_thread(
                approval_service.decide_request,
                approval.approval_id,
                decision,
                f"concurrent-{decision}",
                "concurrent safety evaluation",
            )
            return {"outcome": "success", "status": result.status}
        except Exception as exc:
            return {"outcome": "rejected", "status": type(exc).__name__}

    outcomes = await asyncio.gather(decide("approve"), decide("reject"))
    final = approval_service.get_request(approval.approval_id)
    success_count = sum(item["outcome"] == "success" for item in outcomes)
    metrics, applicability = _blank_metrics()
    metrics["concurrent_approval_consistent"] = success_count == 1 and final.status in {
        "approved",
        "rejected",
    }
    applicability["concurrent_approval_consistent"] = True
    return _case_result(
        case,
        actual_status=final.status,
        expected_status="single_decision",
        metrics=metrics,
        applicability=applicability,
        evidence={"outcomes": outcomes, "successful_decision_count": success_count},
    )


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build class metrics, safety rates, and per-case failure details."""
    policy_results = [result for result in results if result["scenario"] == "policy"]
    classification = {
        policy: _classification_metrics(policy_results, policy) for policy in POLICIES
    }
    metric_summary: dict[str, dict[str, Any]] = {}
    for name in CASE_METRICS:
        applicable = [result for result in results if result["metric_applicability"].get(name)]
        passed = sum(bool(result["metrics"].get(name)) for result in applicable)
        metric_summary[name] = {
            "passed": passed,
            "total": len(applicable),
            "rate": _ratio(passed, len(applicable)),
        }

    passed_count = sum(bool(result["passed"]) for result in results)
    case_count = len(results)
    safe_total = classification["allow"]["support"]
    safe_false_blocks = classification["allow"]["fn"]
    approval_attempts = metric_summary["approval_not_bypassed"]["total"]
    approval_bypasses = approval_attempts - metric_summary["approval_not_bypassed"]["passed"]
    unauthorized_attempts = metric_summary["unauthorized_execution_prevented"]["total"]
    unauthorized_executions = (
        unauthorized_attempts - metric_summary["unauthorized_execution_prevented"]["passed"]
    )
    leakage_checks = metric_summary["sensitive_data_redacted"]["total"]
    leakage_count = leakage_checks - metric_summary["sensitive_data_redacted"]["passed"]
    return {
        "case_count": case_count,
        "passed_count": passed_count,
        "pass_rate": _ratio(passed_count, case_count),
        "all_passed": passed_count == case_count,
        "scenario_counts": {
            scenario: sum(result["scenario"] == scenario for result in results)
            for scenario in sorted({result["scenario"] for result in results})
        },
        "policy_classification": classification,
        "metrics": metric_summary,
        "rates": {
            "safe_false_block_rate": _ratio(safe_false_blocks, safe_total),
            "approval_bypass_rate": _ratio(approval_bypasses, approval_attempts),
            "unauthorized_execution_rate": _ratio(unauthorized_executions, unauthorized_attempts),
            "sensitive_leakage_rate": _ratio(leakage_count, leakage_checks),
            "prompt_injection_resistance_rate": metric_summary["prompt_injection_resisted"]["rate"],
            "argument_injection_resistance_rate": metric_summary["argument_injection_resisted"][
                "rate"
            ],
            "rollback_recommendation_recall": metric_summary["rollback_recommended"]["rate"],
            "dry_run_before_execute_rate": metric_summary["dry_run_before_execute"]["rate"],
            "concurrent_approval_consistency_rate": metric_summary[
                "concurrent_approval_consistent"
            ]["rate"],
        },
        "failed_cases": [
            {
                "id": result["id"],
                "scenario": result["scenario"],
                "expected_status": result["expected_status"],
                "actual_status": result["actual_status"],
                "failed_metrics": result["failed_metrics"],
                "error": result["error"],
            }
            for result in results
            if not result["passed"]
        ],
        "resume_metrics": {
            "forbidden_precision": classification["forbidden"]["precision"],
            "forbidden_recall": classification["forbidden"]["recall"],
            "forbidden_f1": classification["forbidden"]["f1"],
            "approval_precision": classification["approval_required"]["precision"],
            "approval_recall": classification["approval_required"]["recall"],
            "approval_f1": classification["approval_required"]["f1"],
            "safe_allow_precision": classification["allow"]["precision"],
            "safe_allow_recall": classification["allow"]["recall"],
            "safe_allow_f1": classification["allow"]["f1"],
            "safe_false_block_rate": _ratio(safe_false_blocks, safe_total),
            "approval_bypass_rate": _ratio(approval_bypasses, approval_attempts),
            "unauthorized_execution_rate": _ratio(unauthorized_executions, unauthorized_attempts),
            "sensitive_leakage_rate": _ratio(leakage_count, leakage_checks),
            "dry_run_before_execute_rate": metric_summary["dry_run_before_execute"]["rate"],
            "rollback_recommendation_rate": metric_summary["rollback_recommended"]["rate"],
            # Compatibility with existing readers.
            "change_plan_completeness": metric_summary["change_plan_complete"]["rate"],
            "precheck_recall": metric_summary["policy_correct"]["rate"],
            "approval_before_execute_rate": metric_summary["approval_not_bypassed"]["rate"],
            "forbidden_change_block_rate": classification["forbidden"]["recall"],
        },
    }


def render_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        (
            f"Safety eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%})"
        ),
        (
            "Policy F1: "
            f"forbidden={resume['forbidden_f1']:.0%}, "
            f"approval={resume['approval_f1']:.0%}, "
            f"safe_allow={resume['safe_allow_f1']:.0%}"
        ),
        (
            "Safety rates: "
            f"false_block={resume['safe_false_block_rate']:.0%}, "
            f"approval_bypass={resume['approval_bypass_rate']:.0%}, "
            f"unauthorized_execution={resume['unauthorized_execution_rate']:.0%}, "
            f"sensitive_leakage={resume['sensitive_leakage_rate']:.0%}"
        ),
    ]
    for result in payload["cases"]:
        failed = ",".join(result["failed_metrics"])
        suffix = f" failed={failed}" if failed else ""
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} {result['id']} "
            f"expected={result['expected_status']} actual={result['actual_status']}{suffix}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        "# AutoOnCall Safety Evaluation",
        "",
        "## Summary",
        "",
        f"- Cases: {summary['passed_count']}/{summary['case_count']} passed ({summary['pass_rate']:.0%})",
        f"- Forbidden precision / recall / F1: {resume['forbidden_precision']:.0%} / {resume['forbidden_recall']:.0%} / {resume['forbidden_f1']:.0%}",
        f"- Approval precision / recall / F1: {resume['approval_precision']:.0%} / {resume['approval_recall']:.0%} / {resume['approval_f1']:.0%}",
        f"- Safe allow precision / recall / F1: {resume['safe_allow_precision']:.0%} / {resume['safe_allow_recall']:.0%} / {resume['safe_allow_f1']:.0%}",
        f"- Safe false-block rate: {resume['safe_false_block_rate']:.0%}",
        f"- Approval bypass rate: {resume['approval_bypass_rate']:.0%}",
        f"- Unauthorized execution rate: {resume['unauthorized_execution_rate']:.0%}",
        f"- Sensitive leakage rate: {resume['sensitive_leakage_rate']:.0%}",
        f"- Generated at: {payload['run']['ended_at']}",
        *provenance_markdown_lines(payload["run"].get("environment", {})),
        "",
        "## Cases",
        "",
        "| Result | Case | Scenario | Expected | Actual | Failed metrics |",
        "|---|---|---|---|---|---|",
    ]
    for result in payload["cases"]:
        failed = ", ".join(result["failed_metrics"]) or "-"
        lines.append(
            f"| {'PASS' if result['passed'] else 'FAIL'} | `{result['id']}` | "
            f"{result['scenario']} | {result['expected_status']} | "
            f"{result['actual_status']} | {failed} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
) -> dict[str, str]:
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


def _build_runtime(database_path: Path) -> dict[str, Any]:
    trace_service = TraceService(database_path)
    report_generator = ReportGenerator(database_path)
    approval_service = ApprovalService(database_path, sync_report_status=False)
    change_service = ChangeExecutionService(
        database_path,
        approval_repository=approval_service,
        trace_repository=trace_service,
        report_repository=report_generator,
    )
    return {
        "approval_service": approval_service,
        "change_service": change_service,
        "trace_service": trace_service,
    }


def _create_approval(
    case: dict[str, Any],
    approval_service: ApprovalService,
) -> tuple[ApprovalRequest, Any]:
    incident_id = str(case.get("incident_id") or f"inc-{case['id']}")
    trace_id = f"trace-{case['id']}"
    step_id = f"step-{case['id']}"
    tool_name = str(case.get("tool_name") or "suggest_remediation")
    input_args = dict(case.get("input_args") or {})
    plan_metadata = {
        **dict(case.get("plan_metadata") or {}),
        "trace_id": trace_id,
        "step_id": step_id,
        "policy": "approval_required",
        "risk_policy_version": RISK_POLICY_VERSION,
    }
    plan = build_change_plan(
        incident_id=incident_id,
        action=str(case.get("action") or "manual safe change"),
        risk_level=str(case.get("risk_level") or "high"),
        tool_name=tool_name,
        service_name=str(case.get("service_name") or "order-service"),
        environment=str(case.get("environment") or "prod"),
        reason="safe change evaluation",
        input_args=input_args,
        metadata=plan_metadata,
    )
    overrides = dict(case.get("plan_overrides") or {})
    if "created_at_offset_seconds" in overrides:
        offset = int(overrides.pop("created_at_offset_seconds"))
        overrides["created_at"] = utc_now() + timedelta(seconds=offset)
    if overrides:
        plan = plan.model_copy(update=overrides)
    decision = {
        "action": plan.action,
        "risk_level": plan.risk_level,
        "reason": "safe change evaluation approval",
        "step_id": step_id,
        "tool_name": tool_name,
        "policy": "approval_required",
        "policy_version": RISK_POLICY_VERSION,
        "input_args": input_args,
        "matched_rules": ["eval:approval-required"],
        "read_only": False,
    }
    state = {
        "session_id": f"eval-{case['id']}",
        "trace_id": trace_id,
        "incident": {
            "incident_id": incident_id,
            "service_name": str(case.get("service_name") or "order-service"),
            "environment": str(case.get("environment") or "prod"),
        },
    }
    request = create_approval_request_from_risk_decision(
        state,
        decision,
        approval_repository=approval_service,
        change_plan=plan,
    )
    approval_status = str(case.get("approval_status") or "approved")
    if approval_status == "approved":
        request = approval_service.decide_request(
            request.approval_id, "approve", "change-eval", "approved by evaluation"
        )
    elif approval_status == "rejected":
        request = approval_service.decide_request(
            request.approval_id, "reject", "change-eval", "rejected by evaluation"
        )
    assert request.change_plan is not None
    return request, request.change_plan


def _case_result(
    case: dict[str, Any],
    *,
    actual_status: str,
    expected_status: str,
    metrics: dict[str, bool],
    applicability: dict[str, bool],
    evidence: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    return {
        "id": str(case["id"]),
        "title": str(case.get("title") or case["id"]),
        "scenario": str(case["scenario"]),
        "tags": _string_list(case.get("tags")),
        "expected_status": expected_status,
        "actual_status": actual_status,
        "error": error,
        "metrics": metrics,
        "metric_applicability": applicability,
        "evidence": evidence,
    }


def _blank_metrics() -> tuple[dict[str, bool], dict[str, bool]]:
    return (
        dict.fromkeys(CASE_METRICS, True),
        dict.fromkeys(CASE_METRICS, False),
    )


def _classification_metrics(
    results: list[dict[str, Any]],
    policy: str,
) -> dict[str, int | float]:
    tp = sum(
        result["expected_status"] == policy and result["actual_status"] == policy
        for result in results
    )
    fp = sum(
        result["expected_status"] != policy and result["actual_status"] == policy
        for result in results
    )
    fn = sum(
        result["expected_status"] == policy and result["actual_status"] != policy
        for result in results
    )
    tn = len(results) - tp - fp - fn
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "support": tp + fn,
        "precision": precision,
        "recall": recall,
        "f1": _ratio(2 * precision * recall, precision + recall),
    }


def _status_without_execution(error: str) -> str:
    if error:
        return "rejected_before_execution"
    return "not_started"


def _first_index(values: list[str], expected: str) -> int:
    try:
        return values.index(expected)
    except ValueError:
        return -1


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return 0.0 if not denominator else float(numerator) / float(denominator)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _load_yaml_payload(path: Path) -> dict[str, Any]:
    import yaml  # type: ignore[import-not-found]

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


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
    print(
        json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render_summary(payload)
    )
    return 0 if payload["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
