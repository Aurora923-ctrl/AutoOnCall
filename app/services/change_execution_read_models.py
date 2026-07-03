"""Read models for safe change workflow presentation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.models.change_execution import ChangeExecution
from app.services.incident_lifecycle import status_from_change_execution, status_metadata


def build_change_execution_read_model(execution: ChangeExecution) -> dict[str, Any]:
    """Return an API/report friendly view of one safe change execution."""
    payload = execution.model_dump(mode="json")
    lifecycle_status = status_from_change_execution(str(execution.status or ""))
    payload["lifecycle_status"] = lifecycle_status
    payload["status_metadata"] = status_metadata(lifecycle_status)
    payload["stages"] = build_change_execution_stages(payload)
    payload["manual_result_required"] = execution.status == "waiting_manual_execution"
    payload["next_steps"] = change_execution_next_steps([], str(execution.status or ""))
    payload["uncertainties"] = change_execution_uncertainties([], str(execution.status or ""))
    return payload


def build_change_execution_stages(execution: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build the canonical four-stage display timeline for a change execution."""
    status = str(execution.get("status") or "")
    return [
        _stage_from_result(
            key="pre_check",
            label="Pre-check",
            result=execution.get("pre_check"),
            fallback_status="running" if status == "precheck_running" else "pending",
            fallback_reason="未执行",
        ),
        _stage_from_result(
            key="dry_run",
            label="Dry-run",
            result=execution.get("dry_run"),
            fallback_status="running" if status == "dry_run_running" else "pending",
            fallback_reason="未执行",
        ),
        {
            "key": "execute",
            "label": "Execute",
            "status": execution_stage_status(execution),
            "reason": execution_stage_reason(execution),
        },
        _stage_from_result(
            key="observe",
            label="Observe",
            result=execution.get("observation"),
            fallback_status="running" if status == "observing" else "pending",
            fallback_reason="未执行",
        ),
    ]


def execution_stage_status(execution: Mapping[str, Any]) -> str:
    """Return the display status for the production/sandbox execution stage."""
    status = str(execution.get("status") or "")
    manual_result = _as_mapping(execution.get("manual_result"))
    observation = _as_mapping(execution.get("observation"))
    mode = str(execution.get("mode") or "")

    if status == "waiting_manual_execution":
        return "waiting_manual_execution"
    if status == "sandbox_executing":
        return "sandbox_executing"
    if status == "manual_execution_recorded":
        return "manual_execution_recorded"
    if status in {
        "dry_run_completed",
        "sandbox_validated",
        "closed",
        "rollback_recommended",
        "escalated",
    }:
        if manual_result:
            return status
        if mode == "sandbox" or observation:
            return "passed" if status in {"closed", "sandbox_validated"} else status
        return "skipped"
    return "pending"


def execution_stage_reason(execution: Mapping[str, Any]) -> str:
    """Return the display reason for the production/sandbox execution stage."""
    status = str(execution.get("status") or "")
    manual_result = _as_mapping(execution.get("manual_result"))
    rollback_result = _as_mapping(execution.get("rollback_result"))
    observation = _as_mapping(execution.get("observation"))
    mode = str(execution.get("mode") or "")

    if status == "waiting_manual_execution":
        return "dry-run 通过，等待人工提交执行结果。"
    if status == "sandbox_executing":
        return "沙箱执行中，不调用生产写接口。"
    if manual_result:
        return str(
            manual_result.get("notes")
            or f"人工执行结果：{manual_result.get('status') or 'recorded'}"
        )
    if mode == "sandbox" and observation:
        return str(observation.get("recommendation") or "沙箱执行和观察通过，未调用生产写接口。")
    if status == "sandbox_validated":
        return "沙箱执行和观察通过，未调用生产写接口。"
    if status == "dry_run_completed":
        return "dry-run 已完成，未执行生产变更。"
    if status == "closed":
        return "流程已关闭，未自动执行生产变更。"
    if status == "rollback_recommended":
        return str(rollback_result.get("reason") or "观察未通过，建议回滚或升级。")
    if status == "escalated":
        return str(rollback_result.get("reason") or "安全边界阻断，已升级处理。")
    return "未执行"


def change_execution_next_steps(existing: list[str], status: str) -> list[str]:
    """Merge safe-change follow-up guidance into report next steps."""
    steps = list(existing)
    if status in {"precheck_running", "dry_run_running", "sandbox_executing"}:
        steps.append("等待安全变更流程完成当前校验阶段，并持续观察 Trace、指标和日志。")
    elif status == "waiting_manual_execution":
        steps.append("由人工在变更窗口执行已审批计划，并提交执行结果与观察指标。")
    elif status == "rollback_recommended":
        steps.append("按 ChangePlan 回滚步骤处理，并升级给值班负责人复核。")
    elif status == "escalated":
        steps.append("安全变更流程已升级，需人工复核沙箱或生产变更边界。")
    elif status in {"precheck_failed", "dry_run_failed"}:
        steps.append("修正前置检查或 dry-run 阻断项后，重新生成审批和安全变更计划。")
    elif status == "dry_run_completed":
        steps.append("dry-run 已完成且未执行生产变更；如需恢复生产，请走人工执行记录或变更平台。")
    elif status == "sandbox_validated":
        steps.append(
            "sandbox 验证已完成且未执行生产变更；如需恢复生产，请走人工执行记录或变更平台。"
        )
    elif status == "closed":
        steps.append("安全变更流程已关闭，继续按观察窗口复核关键指标是否稳定。")
    return _dedupe_strings(steps)[:8]


def change_execution_uncertainties(existing: list[str], status: str) -> list[str]:
    """Merge safe-change uncertainty notes into diagnosis reports."""
    uncertainties = [
        item for item in existing if "等待人工审批" not in item and "需要人工审批" not in item
    ]
    if status == "waiting_manual_execution":
        uncertainties.append("审批已通过但生产变更仍等待人工执行记录。")
    elif status == "rollback_recommended":
        uncertainties.append("人工执行或观察结果未达标，回滚前需确认影响面。")
    elif status == "escalated":
        uncertainties.append("当前环境不满足安全沙箱执行边界，已转人工复核。")
    elif status in {"precheck_failed", "dry_run_failed"}:
        uncertainties.append("安全变更校验未通过，当前计划不能进入执行阶段。")
    elif status == "dry_run_completed":
        uncertainties.append("dry-run 只验证计划可行性，尚不能证明生产故障已经恢复。")
    elif status == "sandbox_validated":
        uncertainties.append("sandbox 只验证非生产或本地沙箱流程，尚不能证明生产故障已经恢复。")
    elif status == "closed":
        uncertainties = [
            item for item in uncertainties if "等待人工执行" not in item and "人工执行" not in item
        ]
    return _dedupe_strings(uncertainties)[:8]


def _stage_from_result(
    *,
    key: str,
    label: str,
    result: Any,
    fallback_status: str,
    fallback_reason: str,
) -> dict[str, str]:
    stage_result = _as_mapping(result)
    return {
        "key": key,
        "label": label,
        "status": str(stage_result.get("status") or fallback_status or "pending"),
        "reason": str(
            stage_result.get("reason")
            or stage_result.get("recommendation")
            or fallback_reason
            or "未执行"
        ),
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
