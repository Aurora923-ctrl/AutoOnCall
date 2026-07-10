"""Build non-executing change plans for risky AIOps actions."""

from __future__ import annotations

from typing import Any

from app.models.change_plan import ChangePlan, ChangeStep, RemediationPlaybook


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
    context_text = _change_context_text(
        action=action_text,
        tool_name=tool_name,
        reason=reason,
        metadata=metadata or {},
    )
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
        action_type=_infer_action_type(context_text),
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

    observe_metrics = _observe_metrics_for(context_text)
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
        remediation_playbook=_build_remediation_playbook(
            context_text=context_text,
            risk_level=safe_risk,
            service_name=service_text,
            environment=environment_text,
            observe_metrics=observe_metrics,
        ),
        observe_metrics=observe_metrics,
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


def _change_context_text(
    *,
    action: str,
    tool_name: str,
    reason: str,
    metadata: dict[str, Any],
) -> str:
    metadata_parts = [
        f"{key}={value}" for key, value in metadata.items() if key is not None and value is not None
    ]
    return f"{action} {tool_name} {reason} {' '.join(metadata_parts)}".lower()


def _infer_action_type(text: str) -> str:
    if "redis" in text or "maxclients" in text:
        return "redis_config_change"
    if _is_mysql_change(text):
        return "database_change"
    if "restart" in text or "重启" in text:
        return "service_restart"
    if "scale" in text or "扩缩容" in text or "扩容" in text:
        return "capacity_change"
    if "rollback" in text or "回滚" in text:
        return "release_rollback"
    return "manual_change"


def _observe_metrics_for(text: str) -> list[str]:
    if "redis" in text or "maxclients" in text:
        return [
            "redis_connected_clients",
            "redis_rejected_connections",
            "service_5xx_rate",
            "service_p95_latency_ms",
            "redis_timeout_log_count",
        ]
    if _is_mysql_change(text):
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


def _build_remediation_playbook(
    *,
    context_text: str,
    risk_level: str,
    service_name: str,
    environment: str,
    observe_metrics: list[str],
) -> RemediationPlaybook:
    text = context_text.lower()
    approval_required = True
    policy = "approval_required"
    common_precheck = [
        "复核审批单、变更窗口、执行人和回滚负责人。",
        "重新采集最近 10-15 分钟只读指标、日志和依赖状态，确认处置建议仍然成立。",
        "确认当前 runtime 状态与 incident-window 证据边界，避免用历史故障窗口误判当前状态。",
    ]
    common_dry_run = [
        "只生成配置/SQL/运维动作预览，不调用生产写接口。",
        "校验目标服务、环境、参数和 blast radius 与审批内容一致。",
    ]
    common_manual = [
        "审批通过后由人工在变更平台或沙箱环境执行，Agent 只记录执行结果。",
        "执行过程同步记录操作人、时间、命令摘要、观察指标和异常情况。",
    ]
    common_rollback = [
        "任一停止条件触发时停止后续动作，并按审批前配置或容量状态回滚。",
        "回滚后重新采集只读证据，确认错误率、延迟和依赖状态是否收敛。",
    ]
    stop_conditions = [
        "dry-run 输出与审批动作不一致。",
        "核心指标继续恶化或新增高严重级别告警。",
        "只读证据显示当前故障根因已变化。",
    ]

    if "redis" in text or "maxclients" in text:
        return RemediationPlaybook(
            summary=f"Redis maxclients 安全处置草案：{service_name}/{environment}",
            risk_policy=policy,  # type: ignore[arg-type]
            approval_required=approval_required,
            pre_check=[
                *common_precheck,
                "确认 connected_clients、maxclients、rejected_connections 和 Redis timeout 日志仍支持连接耗尽判断。",
            ],
            dry_run=[
                *common_dry_run,
                "预览 maxclients 调整、连接泄漏排查、限流或实例扩容方案，不写入生产 Redis。",
            ],
            sandbox_or_manual_record=[
                *common_manual,
                "人工执行后记录 maxclients、connected_clients、rejected_connections 的前后对比。",
            ],
            rollback=[
                *common_rollback,
                "如连接数未下降或错误率未恢复，回退容量/连接参数并升级应用连接泄漏排查。",
            ],
            observe_metrics=observe_metrics,
            stop_conditions=[
                *stop_conditions,
                "current runtime 已低于阈值且 incident-window 证据无法支撑当前变更。",
            ],
            safety_notes=[
                "Agent 不自动执行 Redis CONFIG SET 或重启实例。",
                "incident-window evidence 与 live_info 必须在报告中分开表达。",
            ],
        )

    if _is_mysql_change(text):
        return RemediationPlaybook(
            summary=f"MySQL 慢查询安全处置草案：{service_name}/{environment}",
            risk_policy=policy,  # type: ignore[arg-type]
            approval_required=approval_required,
            pre_check=[
                *common_precheck,
                "确认慢查询、Threads_running、连接池、P95 和业务错误日志仍支持数据库链路判断。",
            ],
            dry_run=[
                *common_dry_run,
                "预览索引、SQL rewrite、连接池参数或限流方案，不执行生产 DDL/DML。",
            ],
            sandbox_or_manual_record=[
                *common_manual,
                "人工执行后记录 explain、慢查询数量、Threads_running、P95 的前后对比。",
            ],
            rollback=[
                *common_rollback,
                "如延迟或错误率未恢复，按变更平台回滚 SQL/索引/连接池参数并升级 DBA 复核。",
            ],
            observe_metrics=observe_metrics,
            stop_conditions=[
                *stop_conditions,
                "dry-run 显示涉及不可逆 DDL/DML 或超出审批范围的数据变更。",
            ],
            safety_notes=[
                "Agent 不自动执行生产 SQL rewrite、DDL、DML 或数据库参数修改。",
                "诊断阶段只读，所有数据库写操作必须进入审批和人工执行记录。",
            ],
        )

    return RemediationPlaybook(
        summary=f"安全处置草案：{service_name}/{environment}",
        risk_policy=policy,  # type: ignore[arg-type]
        approval_required=approval_required,
        pre_check=common_precheck,
        dry_run=common_dry_run,
        sandbox_or_manual_record=common_manual,
        rollback=common_rollback,
        observe_metrics=observe_metrics,
        stop_conditions=stop_conditions,
        safety_notes=["Agent 只生成变更草案，不自动执行生产写操作。"],
    )


def _is_mysql_change(text: str) -> bool:
    return any(
        token in text
        for token in [
            "mysql",
            "sql",
            "慢查询",
            "连接池",
            "索引",
            "数据库",
            "threads_running",
            "slow query",
        ]
    )
