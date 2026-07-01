"""Offline evaluation for approved safe-change workflow constraints."""

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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent.aiops.risk_controller import assess_plan_step
from app.models.approval import ApprovalRequest
from app.models.change_execution import ManualExecutionResultRequest
from app.models.incident import utc_now
from app.models.plan import PlanStep
from app.services.approval_service import ApprovalService
from app.services.change_execution_service import ChangeExecutionService
from app.services.change_plan_builder import build_change_plan
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "change_cases.yaml"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "change_eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "change_eval_summary.md"

CHANGE_METRIC_NAMES = [
    "change_plan_completeness",
    "precheck_recall",
    "dry_run_before_execute_rate",
    "approval_before_execute_rate",
    "rollback_recommendation_rate",
    "forbidden_change_block_rate",
]

CHANGE_METRIC_FAILURE_REASONS = {
    "change_plan_completeness": "ChangePlan 缺少前置检查、执行步骤、回滚方案或观察指标。",
    "precheck_recall": "pre-check 结果与用例期望不一致。",
    "dry_run_before_execute_rate": "执行阶段没有被 dry-run 保护，或 dry-run 失败后仍进入执行阶段。",
    "approval_before_execute_rate": "审批未通过或仍待审批时进入了安全变更执行记录。",
    "rollback_recommendation_rate": "观察失败后没有进入 rollback_recommended。",
    "forbidden_change_block_rate": "禁止动作进入了安全变更执行流程。",
}


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load safe-change eval cases from YAML."""
    case_path = Path(path)
    payload = _load_yaml_payload(case_path)
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No change eval cases found in {case_path}")
    return [dict(case) for case in cases]


async def evaluate_cases(cases_path: str | Path = DEFAULT_CASES_PATH) -> dict[str, Any]:
    """Evaluate all safe-change cases in an isolated SQLite database."""
    started_at = datetime.now(UTC)
    started_timer = time.perf_counter()
    cases = load_cases(cases_path)
    with TemporaryDirectory(prefix="autooncall-change-eval-") as temp_dir:
        database_path = Path(temp_dir) / "change_eval.db"
        runtime = _build_runtime(database_path)
        results = [await evaluate_case(case, runtime) for case in cases]

    ended_at = datetime.now(UTC)
    summary = build_summary(results)
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic safe-change regression; no production write adapters are used"
            ),
            "cases_path": str(Path(cases_path)),
            "case_ids": [str(case.get("id", "")) for case in cases],
        },
        "summary": summary,
        "cases": results,
    }


async def evaluate_case(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one safe-change case."""
    started = time.perf_counter()
    scenario = str(case.get("scenario") or "safe_change")
    if scenario == "forbidden_policy":
        result = evaluate_forbidden_policy_case(case, runtime)
    else:
        result = await evaluate_safe_change_case(case, runtime)
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result["passed"] = not result["failed_metrics"]
    result["failure_reasons"] = {
        metric: CHANGE_METRIC_FAILURE_REASONS[metric] for metric in result["failed_metrics"]
    }
    return result


async def evaluate_safe_change_case(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate an approval-bound safe-change workflow."""
    approval_service: ApprovalService = runtime["approval_service"]
    change_service: ChangeExecutionService = runtime["change_service"]
    approval, plan = _create_approval(case, approval_service)
    events: list[dict[str, Any]] = []
    error = ""

    try:
        events = [
            event
            async for event in change_service.start_after_approval(
                incident_id=approval.incident_id,
                change_plan_id=plan.change_plan_id,
                approval_id=approval.approval_id,
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
                operator="change-eval",
                notes=str(manual_result.get("notes") or ""),
                observed_metrics=dict(manual_result.get("observed_metrics") or {}),
            ),
        )

    actual_status = execution.status if execution is not None else _status_without_execution(error)
    metrics = _safe_change_metrics(
        case=case,
        approval=approval,
        plan_payload=plan.model_dump(mode="json"),
        execution=execution.model_dump(mode="json") if execution is not None else None,
        events=events,
        error=error,
    )
    return {
        "id": str(case.get("id") or ""),
        "title": str(case.get("title") or case.get("id") or ""),
        "scenario": "safe_change",
        "expected_status": str(case.get("expected_status") or ""),
        "actual_status": actual_status,
        "approval_status": approval.status,
        "event_types": [str(event.get("type") or "") for event in events],
        "error": error,
        "metrics": metrics,
        "failed_metrics": failed_metric_names(metrics),
    }


def evaluate_forbidden_policy_case(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Evaluate that forbidden actions never enter safe-change execution."""
    approval_service: ApprovalService = runtime["approval_service"]
    change_service: ChangeExecutionService = runtime["change_service"]
    incident_id = str(case.get("incident_id") or f"inc-{case.get('id')}")
    step = PlanStep(
        tool_name=str(case.get("tool_name") or "execute_sql"),
        purpose=str(case.get("action") or "危险变更"),
        input_args={"sql": str(case.get("action") or "")},
        expected_evidence="forbidden action should be blocked",
        risk_level="high",
    )
    decision = assess_plan_step(step)
    stored_approvals = approval_service.list_requests(incident_id=incident_id)
    stored_executions = change_service.list_executions(incident_id=incident_id)
    metrics = dict.fromkeys(CHANGE_METRIC_NAMES, True)
    metrics["forbidden_change_block_rate"] = (
        decision.policy == str(case.get("expected_policy") or "forbidden")
        and decision.forbidden
        and not decision.allowed
        and not stored_approvals
        and not stored_executions
    )
    return {
        "id": str(case.get("id") or ""),
        "title": str(case.get("title") or case.get("id") or ""),
        "scenario": "forbidden_policy",
        "expected_status": str(case.get("expected_status") or "forbidden"),
        "actual_status": decision.policy,
        "approval_status": "not_created",
        "event_types": [],
        "error": "",
        "stored_approval_count": len(stored_approvals),
        "stored_execution_count": len(stored_executions),
        "metrics": metrics,
        "failed_metrics": failed_metric_names(metrics),
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build aggregate safe-change metrics."""
    metric_summary = {
        metric: {
            "passed": sum(1 for result in results if result["metrics"].get(metric)),
            "total": len(results),
        }
        for metric in CHANGE_METRIC_NAMES
    }
    passed_count = sum(1 for result in results if result["passed"])
    case_count = len(results)
    for item in metric_summary.values():
        item["rate"] = _ratio(item["passed"], item["total"])
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
                "actual_status": result["actual_status"],
                "error": result["error"],
            }
            for result in results
            if not result["passed"]
        ],
        "resume_metrics": {
            "change_plan_completeness": _metric_rate(metric_summary, "change_plan_completeness"),
            "precheck_recall": _metric_rate(metric_summary, "precheck_recall"),
            "dry_run_before_execute_rate": _metric_rate(
                metric_summary, "dry_run_before_execute_rate"
            ),
            "approval_before_execute_rate": _metric_rate(
                metric_summary, "approval_before_execute_rate"
            ),
            "rollback_recommendation_rate": _metric_rate(
                metric_summary, "rollback_recommendation_rate"
            ),
            "forbidden_change_block_rate": _metric_rate(
                metric_summary, "forbidden_change_block_rate"
            ),
        },
    }


def render_summary(payload: dict[str, Any]) -> str:
    """Render a compact console summary."""
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        (
            f"Safe-change eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%})"
        ),
        (
            "Metrics: "
            f"approval_before_execute={resume['approval_before_execute_rate']:.0%}, "
            f"dry_run_before_execute={resume['dry_run_before_execute_rate']:.0%}, "
            f"rollback={resume['rollback_recommendation_rate']:.0%}, "
            f"forbidden_block={resume['forbidden_change_block_rate']:.0%}"
        ),
    ]
    for result in payload["cases"]:
        suffix = "" if result["passed"] else f" failed={','.join(result['failed_metrics'])}"
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} {result['id']} "
            f"status={result['actual_status']}{suffix}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    """Render a reproducible Markdown safe-change summary."""
    summary = payload["summary"]
    resume = summary["resume_metrics"]
    lines = [
        "# AutoOnCall Safe Change Eval Summary",
        "",
        "## 摘要",
        "",
        f"- 安全变更评测通过率：{summary['passed_count']}/{summary['case_count']} ({summary['pass_rate']:.0%})",
        f"- approval_before_execute_rate：{resume['approval_before_execute_rate']:.0%}",
        f"- dry_run_before_execute_rate：{resume['dry_run_before_execute_rate']:.0%}",
        f"- rollback_recommendation_rate：{resume['rollback_recommendation_rate']:.0%}",
        f"- forbidden_change_block_rate：{resume['forbidden_change_block_rate']:.0%}",
        f"- 生成时间：{payload['run']['ended_at']}",
        "",
        "## 用例",
        "",
    ]
    for result in payload["cases"]:
        failed = ", ".join(result["failed_metrics"]) if result["failed_metrics"] else "-"
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} `{result['id']}`："
            f"status={result['actual_status']}；failed={failed}"
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
    }


def _create_approval(
    case: dict[str, Any],
    approval_service: ApprovalService,
) -> tuple[ApprovalRequest, Any]:
    incident_id = str(case.get("incident_id") or f"inc-{case.get('id')}")
    plan_metadata = {
        "trace_id": f"trace-{case.get('id')}",
        **dict(case.get("plan_metadata") or {}),
    }
    plan = build_change_plan(
        incident_id=incident_id,
        action=str(case.get("action") or "人工执行安全变更"),
        risk_level=str(case.get("risk_level") or "high"),
        tool_name=str(case.get("tool_name") or "suggest_remediation"),
        service_name=str(case.get("service_name") or "order-service"),
        environment=str(case.get("environment") or "prod"),
        reason="safe change eval",
        metadata=plan_metadata,
    )
    overrides = dict(case.get("plan_overrides") or {})
    if "created_at_offset_seconds" in overrides:
        offset = int(overrides.pop("created_at_offset_seconds"))
        overrides["created_at"] = utc_now() + timedelta(seconds=offset)
    if overrides:
        plan = plan.model_copy(update=overrides)

    request = approval_service.create_request(
        ApprovalRequest(
            incident_id=incident_id,
            action=plan.action,
            risk_level=plan.risk_level,
            reason="safe change eval approval",
            change_plan=plan,
            metadata={"trace_id": plan_metadata["trace_id"], "change_plan": plan.model_dump(mode="json")},
        )
    )
    approval_status = str(case.get("approval_status") or "approved")
    if approval_status == "approved":
        request = approval_service.decide_request(
            approval_id=request.approval_id,
            decision="approve",
            decided_by="change-eval",
            reason="approved by eval",
        )
    elif approval_status == "rejected":
        request = approval_service.decide_request(
            approval_id=request.approval_id,
            decision="reject",
            decided_by="change-eval",
            reason="rejected by eval",
        )
    assert request.change_plan is not None
    return request, request.change_plan


def _safe_change_metrics(
    *,
    case: dict[str, Any],
    approval: ApprovalRequest,
    plan_payload: dict[str, Any],
    execution: dict[str, Any] | None,
    events: list[dict[str, Any]],
    error: str,
) -> dict[str, bool]:
    event_types = [str(event.get("type") or "") for event in events]
    expected_status = str(case.get("expected_status") or "")
    expected_precheck = str(case.get("expected_precheck") or "")
    expected_dry_run = str(case.get("expected_dry_run") or "")
    pre_check = dict((execution or {}).get("pre_check") or {})
    dry_run = dict((execution or {}).get("dry_run") or {})
    actual_status = str((execution or {}).get("status") or "")
    expected_approval_block = _is_expected_approval_block(error)

    plan_complete = all(
        [
            plan_payload.get("pre_checklist"),
            plan_payload.get("execution_steps") or plan_payload.get("steps"),
            plan_payload.get("rollback_steps") or plan_payload.get("rollback_plan"),
            plan_payload.get("observe_metrics"),
        ]
    )
    precheck_ok = True if not expected_precheck else pre_check.get("status") == expected_precheck
    dryrun_status_ok = True if not expected_dry_run else dry_run.get("status") == expected_dry_run
    execution_event_index = _first_index(event_types, "change_execution")
    dry_run_event_index = _first_index(event_types, "change_dry_run")
    dryrun_before_execute = (
        execution_event_index == -1
        or (dry_run_event_index != -1 and dry_run_event_index < execution_event_index)
    )
    if dry_run.get("status") == "failed":
        dryrun_before_execute = dryrun_before_execute and execution_event_index == -1

    approval_before_execute = (approval.status == "approved" and not error) or (
        approval.status != "approved" and execution is None and expected_approval_block
    )
    rollback_ok = (
        actual_status == "rollback_recommended"
        if expected_status == "rollback_recommended"
        else True
    )
    status_ok = (
        actual_status == expected_status
        if expected_status not in {"", "rejected_before_execution"}
        else expected_approval_block and execution is None
    )
    return {
        "change_plan_completeness": bool(plan_complete),
        "precheck_recall": bool(precheck_ok and status_ok),
        "dry_run_before_execute_rate": bool(dryrun_status_ok and dryrun_before_execute),
        "approval_before_execute_rate": bool(approval_before_execute),
        "rollback_recommendation_rate": bool(rollback_ok),
        "forbidden_change_block_rate": True,
    }


def _status_without_execution(error: str) -> str:
    if _is_expected_approval_block(error):
        return "rejected_before_execution"
    if error:
        return "error"
    return "not_started"


def _is_expected_approval_block(error: str) -> bool:
    normalized = error.lower()
    return "expected approved" in normalized and (
        "approval is pending" in normalized
        or "approval is rejected" in normalized
        or "approval is cancelled" in normalized
    )


def _first_index(values: list[str], expected: str) -> int:
    try:
        return values.index(expected)
    except ValueError:
        return -1


def failed_metric_names(metrics: dict[str, bool]) -> list[str]:
    """Return metric names that failed."""
    return [name for name in CHANGE_METRIC_NAMES if not metrics.get(name)]


def _metric_rate(metric_summary: dict[str, dict[str, Any]], metric: str) -> float:
    item = metric_summary.get(metric, {})
    return _ratio(int(item.get("passed") or 0), int(item.get("total") or 0))


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return 0.0 if not denominator else float(numerator) / float(denominator)


def _load_yaml_payload(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except ModuleNotFoundError:
        return _parse_simple_yaml(path.read_text(encoding="utf-8"))


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small eval/change_cases.yaml shape when PyYAML is unavailable."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    current_list: list[dict[str, Any]] | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = raw_line.strip()
        if content == "cases:":
            current_list = []
            root["cases"] = current_list
            stack = [(-1, root)]
            continue
        if content.startswith("- "):
            if current_list is None:
                raise ValueError("simple YAML parser only supports top-level cases list")
            item: dict[str, Any] = {}
            current_list.append(item)
            stack = [(-1, root), (indent, item)]
            remainder = content[2:].strip()
            if remainder:
                key, value = _split_yaml_key_value(remainder)
                item[key] = _parse_yaml_scalar(value)
            continue

        key, value = _split_yaml_key_value(content)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_yaml_scalar(value)
    return root


def _split_yaml_key_value(content: str) -> tuple[str, str]:
    if ":" not in content:
        raise ValueError(f"Invalid YAML line: {content}")
    key, value = content.split(":", 1)
    return key.strip(), value.strip()


def _parse_yaml_scalar(value: str) -> Any:
    if value == "":
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


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
