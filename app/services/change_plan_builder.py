"""Build non-executing change plans for risky AIOps actions."""

from __future__ import annotations

from typing import Any

from app.models.change_plan import ChangePlan, ChangeStep


def build_change_plan(
    *,
    incident_id: str,
    action: str,
    risk_level: str,
    tool_name: str = "",
    service_name: str = "unknown-service",
    environment: str = "unknown",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> ChangePlan:
    """Create a human-executed change plan draft without running any action."""
    safe_risk = risk_level if risk_level in {"low", "medium", "high"} else "medium"
    action_text = action or "人工确认后执行处置动作"
    service_text = service_name or "unknown-service"
    environment_text = environment or "unknown"
    rollback_step = ChangeStep(
        action_type="manual_rollback",
        target=service_text,
        tool_name="manual_change_record",
        input_args={"source_action": action_text, "environment": environment_text},
        expected_result="按审批前配置或容量状态完成回滚，并重新采集只读证据。",
        risk_level=safe_risk,  # type: ignore[arg-type]
        requires_approval=True,
        can_dry_run=True,
    )
    execution_step = ChangeStep(
        action_type=_infer_action_type(action_text, tool_name),
        target=service_text,
        tool_name=tool_name or "manual_change_record",
        input_args={
            "action": action_text,
            "service_name": service_text,
            "environment": environment_text,
        },
        expected_result="变更后核心告警收敛，错误率、延迟和依赖超时恢复到可接受范围。",
        risk_level=safe_risk,  # type: ignore[arg-type]
        requires_approval=True,
        can_dry_run=True,
        rollback_step_id=rollback_step.step_id,
    )

    return ChangePlan(
        incident_id=incident_id,
        action=action_text,
        risk_level=safe_risk,  # type: ignore[arg-type]
        pre_checklist=[
            f"确认 incident_id={incident_id}、服务={service_text}、环境和影响范围。",
            "确认最近 10-15 分钟指标、日志、依赖状态和报警仍然支持该变更。",
            "确认变更窗口、审批人、旁路监控和回滚负责人。",
        ],
        execution_steps=[
            "由人工在正式运维平台执行变更，Agent 不自动调用生产写操作。",
            f"按审批记录执行动作：{action_text}。",
            "执行期间持续观察错误率、P95、资源指标和依赖状态。",
        ],
        rollback_steps=[
            "如果核心指标未恢复或继续恶化，立即停止后续变更。",
            "按发布/配置/容量平台回滚到变更前状态。",
            "回滚后重新采集指标、日志和依赖状态，确认影响收敛。",
        ],
        verification_steps=[
            "验证 5xx、P95、QPS、CPU/内存、依赖超时等指标恢复到基线。",
            "验证应用日志中 ERROR、timeout、OOM、慢查询等关键词明显下降。",
            "更新工单和诊断报告，记录执行人、时间、结果和遗留风险。",
        ],
        steps=[execution_step],
        rollback_plan=[rollback_step],
        observe_metrics=_observe_metrics_for(action_text, tool_name),
        blast_radius=f"{environment_text}/{service_text}",
        metadata={
            "tool_name": tool_name,
            "service_name": service_text,
            "environment": environment_text,
            "reason": reason,
            **(metadata or {}),
        },
    )


def update_change_plan_status(plan_payload: dict[str, Any], approval_status: str) -> dict[str, Any]:
    """Update a serialized ChangePlan status after human approval decision."""
    if not plan_payload:
        return {}
    status_map = {
        "approved": "approved",
        "rejected": "rejected",
        "cancelled": "cancelled",
    }
    updated = dict(plan_payload)
    updated["status"] = status_map.get(approval_status, updated.get("status", "draft"))
    updated["manual_execution_required"] = True
    return updated


def _infer_action_type(action: str, tool_name: str) -> str:
    text = f"{action} {tool_name}".lower()
    if "redis" in text or "maxclients" in text:
        return "redis_config_change"
    if "sql" in text or "mysql" in text:
        return "database_change"
    if "restart" in text or "重启" in text:
        return "service_restart"
    if "scale" in text or "扩缩容" in text or "扩容" in text:
        return "capacity_change"
    if "rollback" in text or "回滚" in text:
        return "release_rollback"
    return "manual_change"


def _observe_metrics_for(action: str, tool_name: str) -> list[str]:
    text = f"{action} {tool_name}".lower()
    if "redis" in text or "maxclients" in text:
        return [
            "redis_connected_clients",
            "redis_rejected_connections",
            "service_5xx_rate",
            "service_p95_latency_ms",
            "redis_timeout_log_count",
        ]
    if "mysql" in text or "sql" in text:
        return [
            "mysql_threads_running",
            "mysql_slow_query_count",
            "service_p95_latency_ms",
            "service_5xx_rate",
        ]
    return [
        "service_5xx_rate",
        "service_p95_latency_ms",
        "error_log_count",
        "dependency_timeout_count",
    ]
