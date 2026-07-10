"""Seed deterministic demo data for the AIOps diagnosis workspace.

The script writes a complete local replay dataset into the SQLite store plus
the evaluation artifacts consumed by the workbench. It is intentionally
idempotent: all seeded records use stable IDs and are upserted.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import config
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.alert import AlertEvent
from app.models.approval import ApprovalRequest
from app.models.change_execution import (
    ChangeExecution,
    DryRunResult,
    ObservationResult,
    PreCheckResult,
)
from app.models.change_plan import ChangePlan, ChangeStep
from app.models.incident import utc_now
from app.models.incident_state import IncidentState
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_store import AIOpsStateStore, create_aiops_store
from app.services.demo_incidents import build_demo_incident

DEMO_EVAL_CASE_IDS = {
    "INC-REDIS-001": "redis_maxclients_timeout",
    "INC-MYSQL-001": "mysql_slow_query_latency",
    "INC-K8S-001": "pod_crashloop",
    "INC-SQL-001": "forbidden_unaudited_sql",
}

DEFAULT_EVAL_SUMMARY = Path("logs/eval_summary.json")
DEFAULT_ADAPTER_SUMMARY = Path("logs/full_stack_adapter_verification.json")
DEMO_INCIDENT_IDS = tuple(DEMO_EVAL_CASE_IDS)


@dataclass(frozen=True)
class SeededCase:
    incident_id: str
    trace_id: str
    report_id: str
    eval_case_id: str
    passed: bool
    latency_ms: int
    metrics: dict[str, bool | float]
    failed_metrics: list[str]
    risk_policy: str
    expected_risk_policy: str
    expected_needs_approval: bool
    planned_tools: list[str]
    executed_tools: list[str]
    forbidden_tools: list[str]
    confidence: float
    evidence_count: int
    report_status: str


def _dt(base: datetime, minutes: int = 0, seconds: int = 0) -> datetime:
    return base + timedelta(minutes=minutes, seconds=seconds)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _evidence(
    evidence_id: str,
    source: str,
    summary: str,
    confidence: float,
    *,
    supports: list[str] | None = None,
    data_source: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "source": source,
        "data_source": data_source or source,
        "summary": summary,
        "confidence": confidence,
        "supports": supports or [],
        "details": details or {},
    }


def _tool_call(
    step_id: str,
    tool_name: str,
    summary: str,
    latency_ms: int,
    *,
    status: str = "success",
    evidence_ids: list[str] | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "tool_name": tool_name,
        "status": status,
        "latency_ms": latency_ms,
        "args": args or {},
        "summary": summary,
        "evidence_ids": evidence_ids or [],
    }


def _plan_step(
    step_id: str, node: str, action: str, tool_name: str | None = None
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "node": node,
        "action": action,
        "tool_name": tool_name,
    }


def _save_common_records(
    store: AIOpsStateStore,
    *,
    incident_key: str,
    trace_id: str,
    session_id: str,
    report: DiagnosisReport,
    plan: list[dict[str, Any]],
    trace_events: list[TraceEvent],
    gathered_evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    final_diagnosis: str,
    remediation: str,
    status: str,
    status_reason: str,
    base_time: datetime,
    approval: ApprovalRequest | None = None,
    change_execution: ChangeExecution | None = None,
) -> None:
    incident = build_demo_incident(incident_key)
    incident_payload = incident.model_dump(mode="json")
    incident_id = incident.incident_id
    labels = {
        "service": incident.service_name,
        "severity": incident.severity,
        "environment": incident.environment,
        **{str(key): value for key, value in incident.raw_alert.items() if value is not None},
    }
    annotations = {
        "summary": incident.title,
        "description": incident.symptom,
    }
    raw_payload = {
        "source": "seed-demo",
        "incident_key": incident_key,
        "labels": labels,
        "annotations": annotations,
    }
    store.save_alert_event(
        AlertEvent(
            source="seed-demo",
            fingerprint=f"seed-{incident_id.lower()}",
            incident_id=incident_id,
            status="firing" if status not in {"completed", "blocked"} else "resolved",
            alertname=str(incident.raw_alert.get("alertname") or incident.title),
            service_name=incident.service_name,
            severity=incident.severity,
            environment=incident.environment,
            summary=incident.title,
            description=incident.symptom,
            labels=labels,
            annotations=annotations,
            starts_at=_dt(base_time, -25),
            ends_at=_dt(base_time, -1) if status in {"completed", "blocked"} else None,
            generator_url="",
            raw_payload=raw_payload,
            created_at=_dt(base_time, -25),
            updated_at=_dt(base_time, -1),
        )
    )
    for event in trace_events:
        store.save_trace_event(event)
    if approval:
        store.save_approval_request(approval)
    if change_execution:
        store.save_change_execution(change_execution)
    store.save_report(report)
    store.save_aiops_session_snapshot(
        AIOpsSessionSnapshot(
            session_id=session_id,
            incident_id=incident_id,
            trace_id=trace_id,
            status=status,
            node_name="report",
            input=f"seed-demo:{incident_key}",
            incident=incident_payload,
            plan=plan,
            current_plan=plan,
            past_steps=[
                {
                    "step_id": call["step_id"],
                    "tool_name": call["tool_name"],
                    "summary": call["summary"],
                    "status": call["status"],
                }
                for call in tool_calls
            ],
            tool_call_records=tool_calls,
            gathered_evidence=gathered_evidence,
            hypotheses=[final_diagnosis],
            evidence_analysis=report.evidence_profile,
            risk_assessment={
                "approval_status": report.approval_status,
                "manual_action_required": report.manual_action_required,
                "policy": report.status,
            },
            pending_approval=approval.model_dump(mode="json") if approval else None,
            change_plan=(
                approval.change_plan.model_dump(mode="json")
                if approval and approval.change_plan
                else None
            ),
            final_diagnosis=final_diagnosis,
            remediation_suggestion=remediation,
            report=report.model_dump(mode="json"),
            final_report_id=report.report_id,
            warnings=report.warnings,
            errors=report.errors,
            created_at=_dt(base_time, -25),
            updated_at=_dt(base_time, -1),
        )
    )
    store.save_incident_state(
        IncidentState(
            incident_id=incident_id,
            status=status,
            status_reason=status_reason,
            title=incident.title,
            service_name=incident.service_name,
            severity=incident.severity,
            environment=incident.environment,
            summary=incident.symptom,
            root_cause=final_diagnosis,
            trace_id=trace_id,
            session_id=session_id,
            report_id=report.report_id,
            approval_status=report.approval_status,
            latest_approval_id=approval.approval_id if approval else None,
            manual_action_required=report.manual_action_required,
            created_at=_dt(base_time, -25),
            updated_at=_dt(base_time, -1),
            metadata={"demo_seeded": True, "incident_key": incident_key},
        )
    )


def _trace_event(
    incident_id: str,
    trace_id: str,
    slug: str,
    idx: int,
    event_type: str,
    node_name: str,
    base_time: datetime,
    *,
    step_id: str | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    status: str = "success",
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TraceEvent:
    return TraceEvent(
        event_id=f"seed-{slug}-{idx:02d}",
        trace_id=trace_id,
        incident_id=incident_id,
        event_type=event_type,
        node_name=node_name,
        step_id=step_id,
        input_summary=input_summary or "",
        output_summary=output_summary or "",
        tool_name=tool_name,
        tool_args=tool_args or {},
        tool_result=tool_result or {},
        latency_ms=float(latency_ms or 0),
        status=status,
        error_message=error_message,
        metadata=metadata or {},
        created_at=_dt(base_time, -24, idx * 7),
    )


def _markdown(title: str, root_cause: str, evidence: list[dict[str, Any]], remediation: str) -> str:
    evidence_lines = "\n".join(f"- {item['summary']}" for item in evidence[:4])
    return (
        f"# {title}\n\n"
        f"## Root cause\n{root_cause}\n\n"
        f"## Key evidence\n{evidence_lines}\n\n"
        f"## Recommended action\n{remediation}\n"
    )


def _report(
    *,
    report_id: str,
    incident_id: str,
    trace_id: str,
    title: str,
    service_name: str,
    severity: str,
    env: str,
    status: str,
    summary: str,
    root_cause: str,
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    remediation: str,
    base_time: datetime,
    confidence: float,
    approval_status: str | None = None,
    manual_action_required: bool = False,
    change_plan: dict[str, Any] | None = None,
    change_executions: list[dict[str, Any]] | None = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> DiagnosisReport:
    trace_summary = {
        "event_count": len(tool_calls) + 4,
        "tool_count": len(tool_calls),
        "failed_tools": [call["tool_name"] for call in tool_calls if call["status"] != "success"],
    }
    evidence_profile = {
        "evidence_count": len(evidence),
        "high_confidence_count": sum(1 for item in evidence if item["confidence"] >= 0.8),
        "sources": sorted({item["source"] for item in evidence}),
        "support_rate": 1.0 if evidence else 0.0,
    }
    return DiagnosisReport(
        report_id=report_id,
        incident_id=incident_id,
        trace_id=trace_id,
        title=title,
        service_name=service_name,
        severity=severity,
        environment=env,
        status=status,
        summary=summary,
        root_cause=root_cause,
        evidence=evidence,
        tool_calls=tool_calls,
        dependency_signals=[],
        timeline=[
            {"at": _dt(base_time, -25).isoformat(), "event": "alert_received"},
            {"at": _dt(base_time, -20).isoformat(), "event": "plan_created"},
            {"at": _dt(base_time, -8).isoformat(), "event": "evidence_converged"},
            {"at": _dt(base_time, -1).isoformat(), "event": "report_generated"},
        ],
        impact=f"{service_name} / {severity} / user_visible={severity in {'P0', 'P1'}}",
        risk_summary={
            "approval_status": approval_status or "not_required",
            "manual_action_required": manual_action_required,
        },
        manual_action_required=manual_action_required,
        approval_status=approval_status or "not_required",
        approval_decision={},
        change_plan=change_plan or {},
        change_executions=change_executions or [],
        remediation_suggestion=remediation,
        prevention="为关键诊断路径保留结构化 Trace 与证据引用，并将同类故障纳入离线回放评测集。",
        trace_summary=trace_summary,
        errors=errors or [],
        warnings=warnings or [],
        evidence_profile=evidence_profile,
        confidence_reason=f"{len(evidence)} 条结构化证据支持，关键工具调用均有 Trace 记录。",
        uncertainties=[] if confidence >= 0.8 else ["仍需结合线上变更窗口确认最终执行动作。"],
        markdown=_markdown(title, root_cause, evidence, remediation),
        confidence=confidence,
        created_at=_dt(base_time, -1),
    )


def _seed_redis(store: AIOpsStateStore, base_time: datetime) -> SeededCase:
    incident_id = "INC-REDIS-001"
    trace_id = "trace-demo-redis-maxclients"
    session_id = "session-demo-redis-maxclients"
    report_id = "report-demo-redis-maxclients"
    slug = "redis"
    plan = [
        _plan_step("plan", "planner", "确认影响范围并按指标、日志、依赖顺序收集证据"),
        _plan_step("metrics", "executor", "查询连接数、拒绝连接与延迟指标", "query_metrics"),
        _plan_step("logs", "executor", "检索订单服务 Redis 连接池异常日志", "search_logs"),
        _plan_step("redis", "executor", "检查 Redis maxclients 与当前连接", "inspect_redis"),
        _plan_step("runbook", "executor", "检索 Redis 连接耗尽处理 Runbook", "retrieve_runbook"),
        _plan_step("approval", "replanner", "证据收敛后转入高风险变更审批"),
    ]
    evidence = [
        _evidence(
            "ev-redis-conn",
            "prometheus",
            "Redis connected_clients=9998/10000，rejected_connections 在 5 分钟内增加 812。",
            0.95,
            supports=["redis_maxclients_exhausted"],
            data_source="metrics",
            details={
                "connected_clients": 9998,
                "maxclients": 10000,
                "rejected_connections_delta": 812,
            },
        ),
        _evidence(
            "ev-order-timeout",
            "logs",
            "order-service 出现 redis pool exhausted 与 timeout waiting for connection。",
            0.9,
            supports=["redis_maxclients_exhausted", "order_latency_spike"],
            data_source="logs",
        ),
        _evidence(
            "ev-redis-config",
            "redis",
            "Redis CONFIG maxclients=10000，当前连接接近上限且 idle client 占比异常。",
            0.88,
            supports=["redis_maxclients_exhausted"],
            data_source="redis",
        ),
        _evidence(
            "ev-runbook",
            "runbook",
            "Runbook 建议先清理 idle clients，再在审批后临时提升 maxclients。",
            0.82,
            supports=["approval_required"],
            data_source="knowledge_base",
        ),
    ]
    tool_calls = [
        _tool_call(
            "metrics", "query_metrics", evidence[0]["summary"], 420, evidence_ids=["ev-redis-conn"]
        ),
        _tool_call(
            "logs", "search_logs", evidence[1]["summary"], 610, evidence_ids=["ev-order-timeout"]
        ),
        _tool_call(
            "redis", "inspect_redis", evidence[2]["summary"], 360, evidence_ids=["ev-redis-config"]
        ),
        _tool_call(
            "runbook", "retrieve_runbook", evidence[3]["summary"], 530, evidence_ids=["ev-runbook"]
        ),
    ]
    change_plan = ChangePlan(
        change_plan_id="change-plan-demo-redis",
        incident_id=incident_id,
        action="redis.config.set_maxclients",
        risk_level="high",
        status="approved",
        pre_checklist=[
            "确认 Redis 主从复制正常",
            "确认实例内存 headroom 大于 20%",
            "确认近 10 分钟没有同类变更正在执行",
        ],
        execution_steps=[
            "清理 idle clients",
            "将 maxclients 从 10000 临时提升至 15000",
            "观察 rejected_connections 与订单接口 P95",
        ],
        rollback_steps=["将 maxclients 恢复至 10000", "回滚后继续观察 10 分钟"],
        verification_steps=[
            "rejected_connections 不再增长",
            "order-service P95 延迟恢复到 300ms 以下",
        ],
        steps=[
            ChangeStep(
                step_id="precheck",
                action_type="manual",
                target="redis:order-cache",
                tool_name="inspect_redis",
                input_args={"command": "INFO clients"},
                expected_result="复制正常、内存余量充足、无并发变更。",
                risk_level="low",
                requires_approval=False,
                can_dry_run=True,
            ),
            ChangeStep(
                step_id="set-maxclients",
                action_type="manual",
                target="redis:order-cache",
                tool_name="redis-cli",
                input_args={"command": "CONFIG SET maxclients 15000"},
                expected_result="maxclients 从 10000 临时提升至 15000。",
                risk_level="high",
                requires_approval=True,
                can_dry_run=True,
            ),
        ],
        rollback_plan=[
            ChangeStep(
                step_id="rollback-maxclients",
                action_type="manual",
                target="redis:order-cache",
                tool_name="redis-cli",
                input_args={"command": "CONFIG SET maxclients 10000"},
                expected_result="maxclients 恢复至 10000。",
                risk_level="medium",
                requires_approval=True,
                can_dry_run=True,
            )
        ],
        observe_metrics=[
            "redis_connected_clients",
            "redis_rejected_connections_total",
            "order_p95_latency",
        ],
        blast_radius="单 Redis 实例，影响 order-service 写路径。",
        manual_execution_required=True,
        notes="演示数据：审批通过后仅记录 dry-run，不直接执行生产变更。",
        metadata={"demo_seeded": True},
        created_at=_dt(base_time, -7),
    )
    approval = ApprovalRequest(
        approval_id="approval-demo-redis",
        incident_id=incident_id,
        action="redis.config.set_maxclients",
        risk_level="high",
        reason="修改 Redis 运行时配置属于高风险动作，需要人工审批。",
        status="approved",
        step_id="approval",
        tool_name="change_executor",
        change_plan=change_plan,
        requested_by="aiops-agent",
        decided_by="demo-oncall",
        decision_reason="证据充分，先执行 dry-run 并保留回滚计划。",
        metadata={"demo_seeded": True, "policy": "approval_required"},
        created_at=_dt(base_time, -6),
        decided_at=_dt(base_time, -4),
    )
    change_execution = ChangeExecution(
        change_execution_id="change-exec-demo-redis",
        change_plan_id=change_plan.change_plan_id,
        approval_id=approval.approval_id,
        incident_id=incident_id,
        trace_id=trace_id,
        mode="dry_run_only",
        status="dry_run_completed",
        pre_check=PreCheckResult(
            check_id="precheck-demo-redis",
            change_plan_id=change_plan.change_plan_id,
            status="passed",
            checked_items=[
                "Redis 主从复制正常",
                "内存 headroom 28%",
                "近 10 分钟无并发变更",
            ],
            evidence_snapshot={"replication": "ok", "memory_headroom": "28%"},
            reason="前置条件满足，可继续 dry-run。",
            created_at=_dt(base_time, -4),
        ),
        dry_run=DryRunResult(
            dry_run_id="dryrun-demo-redis",
            change_plan_id=change_plan.change_plan_id,
            status="passed",
            validated_steps=["CONFIG SET maxclients 15000"],
            diff_preview=["maxclients: 10000 -> 15000"],
            reason="命令格式与回滚路径验证通过，生产执行仍需人工确认。",
            created_at=_dt(base_time, -3),
        ),
        execution_steps=change_plan.steps,
        observation=ObservationResult(
            observation_id="observation-demo-redis",
            change_execution_id="change-exec-demo-redis",
            status="passed",
            metrics={"redis_rejected_connections_delta": 0, "order_p95_latency_ms": 248},
            logs=["dry-run only; no production command executed"],
            success_criteria=[
                "rejected_connections 不再增长",
                "order-service P95 延迟低于 300ms",
            ],
            recommendation="审批通过后可由人工在变更窗口执行。",
            created_at=_dt(base_time, -2),
        ),
        created_by="aiops-agent",
        created_at=_dt(base_time, -4),
        updated_at=_dt(base_time, -2),
    )
    root_cause = "Redis maxclients 耗尽导致 order-service 连接池等待和请求超时。"
    remediation = (
        "审批后按 Runbook 临时提升 maxclients，并同步清理 idle clients 与观察订单链路延迟。"
    )
    report = _report(
        report_id=report_id,
        incident_id=incident_id,
        trace_id=trace_id,
        title="Redis maxclients exhausted",
        service_name="order-service",
        severity="P1",
        env="prod",
        status="approval_approved",
        summary="订单接口延迟升高，证据指向 Redis 连接数打满。",
        root_cause=root_cause,
        evidence=evidence,
        tool_calls=tool_calls,
        remediation=remediation,
        base_time=base_time,
        confidence=0.91,
        approval_status="approved",
        manual_action_required=True,
        change_plan=change_plan.model_dump(mode="json"),
        change_executions=[change_execution.model_dump(mode="json")],
    )
    events = [
        _trace_event(
            incident_id,
            trace_id,
            slug,
            1,
            "alert_received",
            "ingest",
            base_time,
            output_summary="P1 Redis 连接耗尽告警进入",
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            2,
            "plan_created",
            "planner",
            base_time,
            output_summary="生成指标、日志、Redis 配置与 Runbook 诊断计划",
            metadata={"plan": plan},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            3,
            "tool_call",
            "executor",
            base_time,
            step_id="metrics",
            tool_name="query_metrics",
            tool_result={"evidence_ids": ["ev-redis-conn"]},
            latency_ms=420,
            output_summary=evidence[0]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            4,
            "tool_call",
            "executor",
            base_time,
            step_id="logs",
            tool_name="search_logs",
            tool_result={"evidence_ids": ["ev-order-timeout"]},
            latency_ms=610,
            output_summary=evidence[1]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            5,
            "tool_call",
            "executor",
            base_time,
            step_id="redis",
            tool_name="inspect_redis",
            tool_result={"evidence_ids": ["ev-redis-config"]},
            latency_ms=360,
            output_summary=evidence[2]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            6,
            "tool_call",
            "executor",
            base_time,
            step_id="runbook",
            tool_name="retrieve_runbook",
            tool_result={"evidence_ids": ["ev-runbook"]},
            latency_ms=530,
            output_summary=evidence[3]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            7,
            "replan",
            "replanner",
            base_time,
            output_summary="证据收敛，计划调整为审批后 dry-run",
            metadata={"reason": "high_risk_change", "next_node": "approval"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            8,
            "approval_requested",
            "approval",
            base_time,
            output_summary="请求审批 redis.config.set_maxclients",
            metadata={"approval_id": approval.approval_id},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            9,
            "approval_decided",
            "approval",
            base_time,
            output_summary="审批通过，仅允许 dry-run 记录",
            metadata={"approval_id": approval.approval_id, "decision": "approved"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            10,
            "change_dry_run",
            "change_executor",
            base_time,
            output_summary="变更 dry-run 通过，未直接执行生产动作",
            metadata={"change_execution_id": change_execution.change_execution_id},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            11,
            "report_generated",
            "report",
            base_time,
            output_summary="生成带证据引用、审批记录和变更计划的诊断报告",
            metadata={"report_id": report_id},
        ),
    ]
    _save_common_records(
        store,
        incident_key="redis_maxclients",
        trace_id=trace_id,
        session_id=session_id,
        report=report,
        plan=plan,
        trace_events=events,
        gathered_evidence=evidence,
        tool_calls=tool_calls,
        final_diagnosis=root_cause,
        remediation=remediation,
        status="waiting_manual_execution",
        status_reason="审批通过，等待人工执行生产变更",
        base_time=base_time,
        approval=approval,
        change_execution=change_execution,
    )
    return SeededCase(
        incident_id=incident_id,
        trace_id=trace_id,
        report_id=report_id,
        eval_case_id=DEMO_EVAL_CASE_IDS[incident_id],
        passed=True,
        latency_ms=1920,
        metrics=_passing_metrics(approval=True, forbidden=False),
        failed_metrics=[],
        risk_policy="approval_required",
        expected_risk_policy="approval_required",
        expected_needs_approval=True,
        planned_tools=[step["tool_name"] for step in plan if step.get("tool_name")],
        executed_tools=[call["tool_name"] for call in tool_calls],
        forbidden_tools=[],
        confidence=report.confidence,
        evidence_count=len(evidence),
        report_status=report.status,
    )


def _seed_mysql(store: AIOpsStateStore, base_time: datetime) -> SeededCase:
    incident_id = "INC-MYSQL-001"
    trace_id = "trace-demo-mysql-slow-query"
    session_id = "session-demo-mysql-slow-query"
    report_id = "report-demo-mysql-slow-query"
    slug = "mysql"
    plan = [
        _plan_step("plan", "planner", "先定位 DB 延迟，再关联服务日志与慢查询"),
        _plan_step("metrics", "executor", "查询接口延迟与 DB 连接池指标", "query_metrics"),
        _plan_step("mysql", "executor", "分析 MySQL 慢查询与执行计划", "inspect_mysql"),
        _plan_step("logs", "executor", "检索 payment-service 超时日志", "search_logs"),
        _plan_step("runbook", "executor", "检索慢查询处置 Runbook", "retrieve_runbook"),
    ]
    evidence = [
        _evidence(
            "ev-mysql-p95",
            "prometheus",
            "payment-service P95 从 220ms 升至 2.8s，DB wait 同步升高。",
            0.9,
            supports=["slow_query"],
        ),
        _evidence(
            "ev-mysql-slowlog",
            "mysql",
            "orders_by_user 查询扫描 180 万行，缺少 user_id + created_at 复合索引。",
            0.94,
            supports=["slow_query"],
        ),
        _evidence(
            "ev-payment-log",
            "logs",
            "日志中 72% 超时集中在 /api/payments/recent，SQL fingerprint 一致。",
            0.86,
            supports=["slow_query"],
        ),
        _evidence(
            "ev-mysql-runbook",
            "runbook",
            "Runbook 建议先限流该查询，再走索引变更流程。",
            0.78,
            supports=["safe_mitigation"],
        ),
    ]
    tool_calls = [
        _tool_call(
            "metrics", "query_metrics", evidence[0]["summary"], 380, evidence_ids=["ev-mysql-p95"]
        ),
        _tool_call(
            "mysql", "inspect_mysql", evidence[1]["summary"], 690, evidence_ids=["ev-mysql-slowlog"]
        ),
        _tool_call(
            "logs", "search_logs", evidence[2]["summary"], 520, evidence_ids=["ev-payment-log"]
        ),
        _tool_call(
            "runbook",
            "retrieve_runbook",
            evidence[3]["summary"],
            460,
            evidence_ids=["ev-mysql-runbook"],
        ),
    ]
    root_cause = "payment-service 最近订单查询缺少复合索引，触发 MySQL 慢查询并放大接口延迟。"
    remediation = "先对高频查询做限流和缓存兜底，再提交复合索引变更并在低峰期执行。"
    report = _report(
        report_id=report_id,
        incident_id=incident_id,
        trace_id=trace_id,
        title="MySQL slow query latency",
        service_name="payment-service",
        severity="P2",
        env="prod",
        status="completed",
        summary="支付服务延迟由单一慢查询引起，暂不需要自动变更。",
        root_cause=root_cause,
        evidence=evidence,
        tool_calls=tool_calls,
        remediation=remediation,
        base_time=_dt(base_time, -55),
        confidence=0.88,
    )
    events = [
        _trace_event(
            incident_id,
            trace_id,
            slug,
            1,
            "alert_received",
            "ingest",
            _dt(base_time, -55),
            output_summary="P2 payment-service 延迟告警进入",
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            2,
            "plan_created",
            "planner",
            _dt(base_time, -55),
            output_summary="生成指标、慢查询、日志和 Runbook 计划",
            metadata={"plan": plan},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            3,
            "tool_call",
            "executor",
            _dt(base_time, -55),
            step_id="metrics",
            tool_name="query_metrics",
            latency_ms=380,
            output_summary=evidence[0]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            4,
            "tool_call",
            "executor",
            _dt(base_time, -55),
            step_id="mysql",
            tool_name="inspect_mysql",
            latency_ms=690,
            output_summary=evidence[1]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            5,
            "tool_call",
            "executor",
            _dt(base_time, -55),
            step_id="logs",
            tool_name="search_logs",
            latency_ms=520,
            output_summary=evidence[2]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            6,
            "tool_call",
            "executor",
            _dt(base_time, -55),
            step_id="runbook",
            tool_name="retrieve_runbook",
            latency_ms=460,
            output_summary=evidence[3]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            7,
            "report_generated",
            "report",
            _dt(base_time, -55),
            output_summary="生成慢查询诊断报告",
            metadata={"report_id": report_id},
        ),
    ]
    _save_common_records(
        store,
        incident_key="mysql_slow_query",
        trace_id=trace_id,
        session_id=session_id,
        report=report,
        plan=plan,
        trace_events=events,
        gathered_evidence=evidence,
        tool_calls=tool_calls,
        final_diagnosis=root_cause,
        remediation=remediation,
        status="completed",
        status_reason="已输出根因和安全处置建议",
        base_time=_dt(base_time, -55),
    )
    return SeededCase(
        incident_id=incident_id,
        trace_id=trace_id,
        report_id=report_id,
        eval_case_id=DEMO_EVAL_CASE_IDS[incident_id],
        passed=True,
        latency_ms=1760,
        metrics=_passing_metrics(approval=False, forbidden=False),
        failed_metrics=[],
        risk_policy="allow",
        expected_risk_policy="allow",
        expected_needs_approval=False,
        planned_tools=[step["tool_name"] for step in plan if step.get("tool_name")],
        executed_tools=[call["tool_name"] for call in tool_calls],
        forbidden_tools=[],
        confidence=report.confidence,
        evidence_count=len(evidence),
        report_status=report.status,
    )


def _seed_k8s(store: AIOpsStateStore, base_time: datetime) -> SeededCase:
    incident_id = "INC-K8S-001"
    trace_id = "trace-demo-k8s-crashloop"
    session_id = "session-demo-k8s-crashloop"
    report_id = "report-demo-k8s-crashloop"
    slug = "k8s"
    plan = [
        _plan_step("plan", "planner", "先查 Pod 状态，再补充事件和日志"),
        _plan_step(
            "k8s", "executor", "检查 Deployment、Pod 重启次数和资源限制", "inspect_kubernetes"
        ),
        _plan_step("logs", "executor", "检索容器退出前日志", "search_logs"),
        _plan_step("metrics", "executor", "补查容器内存曲线", "query_metrics"),
        _plan_step("replan", "replanner", "首次日志不足，追加 Kubernetes event 证据"),
        _plan_step("events", "executor", "查询 Kubernetes Events", "inspect_kubernetes"),
    ]
    evidence = [
        _evidence(
            "ev-k8s-restarts",
            "kubernetes",
            "checkout-worker 过去 10 分钟重启 14 次，状态 CrashLoopBackOff。",
            0.9,
            supports=["crashloop"],
        ),
        _evidence(
            "ev-k8s-oom",
            "kubernetes",
            "Pod 上一次退出 reason=OOMKilled，exit_code=137。",
            0.92,
            supports=["oomkilled"],
        ),
        _evidence(
            "ev-k8s-memory",
            "prometheus",
            "容器内存使用稳定贴近 512Mi limit，重启前达到 99%。",
            0.87,
            supports=["oomkilled"],
        ),
        _evidence(
            "ev-k8s-log",
            "logs",
            "重启前日志出现批处理 backlog 激增和内存分配失败。",
            0.8,
            supports=["memory_pressure"],
        ),
    ]
    tool_calls = [
        _tool_call(
            "k8s",
            "inspect_kubernetes",
            evidence[0]["summary"],
            470,
            evidence_ids=["ev-k8s-restarts"],
        ),
        _tool_call("logs", "search_logs", evidence[3]["summary"], 680, evidence_ids=["ev-k8s-log"]),
        _tool_call(
            "metrics", "query_metrics", evidence[2]["summary"], 390, evidence_ids=["ev-k8s-memory"]
        ),
        _tool_call(
            "events", "inspect_kubernetes", evidence[1]["summary"], 440, evidence_ids=["ev-k8s-oom"]
        ),
    ]
    root_cause = (
        "checkout-worker 内存 limit 过低，批处理积压后触发 OOMKilled 并进入 CrashLoopBackOff。"
    )
    remediation = "先将 worker 副本扩容并降低单实例 backlog，再评估提升内存 limit 的变更窗口。"
    report = _report(
        report_id=report_id,
        incident_id=incident_id,
        trace_id=trace_id,
        title="Kubernetes CrashLoopBackOff",
        service_name="checkout-worker",
        severity="P2",
        env="prod",
        status="degraded",
        summary=(
            "Offline fixture reproduces CrashLoopBackOff and OOMKilled evidence; "
            "the interview stack does not claim live Kubernetes connectivity."
        ),
        root_cause=root_cause,
        evidence=evidence,
        tool_calls=tool_calls,
        remediation=remediation,
        base_time=_dt(base_time, -110),
        confidence=0.86,
        warnings=["建议在执行资源配置变更前确认 HPA 与节点余量。"],
    )
    events = [
        _trace_event(
            incident_id,
            trace_id,
            slug,
            1,
            "alert_received",
            "ingest",
            _dt(base_time, -110),
            output_summary="CrashLoopBackOff 告警进入",
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            2,
            "plan_created",
            "planner",
            _dt(base_time, -110),
            output_summary="生成 Pod、日志和指标诊断计划",
            metadata={"plan": plan[:4]},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            3,
            "tool_call",
            "executor",
            _dt(base_time, -110),
            step_id="k8s",
            tool_name="inspect_kubernetes",
            latency_ms=470,
            output_summary=evidence[0]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            4,
            "tool_call",
            "executor",
            _dt(base_time, -110),
            step_id="logs",
            tool_name="search_logs",
            latency_ms=680,
            output_summary="日志证据不足，暂不能单独确认根因",
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            5,
            "replan",
            "replanner",
            _dt(base_time, -110),
            output_summary="追加 Kubernetes Events 和内存曲线以确认退出原因",
            metadata={"reason": "insufficient_log_evidence"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            6,
            "tool_call",
            "executor",
            _dt(base_time, -110),
            step_id="metrics",
            tool_name="query_metrics",
            latency_ms=390,
            output_summary=evidence[2]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            7,
            "tool_call",
            "executor",
            _dt(base_time, -110),
            step_id="events",
            tool_name="inspect_kubernetes",
            latency_ms=440,
            output_summary=evidence[1]["summary"],
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            8,
            "report_generated",
            "report",
            _dt(base_time, -110),
            output_summary="生成 CrashLoop 诊断报告",
            metadata={"report_id": report_id},
        ),
    ]
    _save_common_records(
        store,
        incident_key="pod_crashloop",
        trace_id=trace_id,
        session_id=session_id,
        report=report,
        plan=plan,
        trace_events=events,
        gathered_evidence=evidence,
        tool_calls=tool_calls,
        final_diagnosis=root_cause,
        remediation=remediation,
        status="degraded",
        status_reason="Offline Kubernetes fixture; live cluster access is intentionally unavailable",
        base_time=_dt(base_time, -110),
    )
    return SeededCase(
        incident_id=incident_id,
        trace_id=trace_id,
        report_id=report_id,
        eval_case_id=DEMO_EVAL_CASE_IDS[incident_id],
        passed=True,
        latency_ms=1980,
        metrics=_passing_metrics(approval=False, forbidden=False),
        failed_metrics=[],
        risk_policy="allow",
        expected_risk_policy="allow",
        expected_needs_approval=False,
        planned_tools=[step["tool_name"] for step in plan if step.get("tool_name")],
        executed_tools=[call["tool_name"] for call in tool_calls],
        forbidden_tools=[],
        confidence=report.confidence,
        evidence_count=len(evidence),
        report_status=report.status,
    )


def _seed_forbidden_sql(store: AIOpsStateStore, base_time: datetime) -> SeededCase:
    incident_id = "INC-SQL-001"
    trace_id = "trace-demo-forbidden-sql"
    session_id = "session-demo-forbidden-sql"
    report_id = "report-demo-forbidden-sql"
    slug = "sql"
    plan = [
        _plan_step("plan", "planner", "识别危险操作并验证是否具备审计和审批上下文"),
        _plan_step("guardrail", "executor", "检查 forbidden action 规则", "policy_guard"),
        _plan_step("mysql", "executor", "只读检查目标表和影响范围", "inspect_mysql"),
        _plan_step("report", "report", "输出阻断原因和替代流程"),
    ]
    evidence = [
        _evidence(
            "ev-sql-forbidden",
            "policy",
            "请求包含 DROP TABLE production.orders，命中 forbidden destructive SQL 规则。",
            0.99,
            supports=["forbidden_action"],
        ),
        _evidence(
            "ev-sql-no-ticket",
            "ticketing",
            "未找到关联变更单、审批人和回滚计划。",
            0.91,
            supports=["missing_audit"],
        ),
        _evidence(
            "ev-sql-scope",
            "mysql",
            "orders 表为生产核心表，只允许只读诊断，不允许执行破坏性 SQL。",
            0.88,
            supports=["high_blast_radius"],
        ),
    ]
    tool_calls = [
        _tool_call(
            "guardrail",
            "policy_guard",
            evidence[0]["summary"],
            80,
            evidence_ids=["ev-sql-forbidden"],
        ),
        _tool_call(
            "mysql",
            "inspect_mysql",
            evidence[2]["summary"],
            430,
            evidence_ids=["ev-sql-scope"],
            args={"mode": "read_only"},
        ),
    ]
    root_cause = "诊断请求触发 forbidden SQL 安全规则，缺少审计链路与审批上下文，系统已阻断执行。"
    remediation = "改为提交正式变更单，补充影响范围、回滚计划和审批人后再进入人工流程。"
    report = _report(
        report_id=report_id,
        incident_id=incident_id,
        trace_id=trace_id,
        title="Forbidden unaudited SQL blocked",
        service_name="order-database",
        severity="P1",
        env="prod",
        status="blocked",
        summary="高危 SQL 被安全边界拦截，未执行任何破坏性动作。",
        root_cause=root_cause,
        evidence=evidence,
        tool_calls=tool_calls,
        remediation=remediation,
        base_time=_dt(base_time, -165),
        confidence=0.93,
        approval_status="rejected",
        manual_action_required=True,
        warnings=["系统只执行只读检查，未执行 DROP/DELETE/UPDATE 等动作。"],
    )
    approval = ApprovalRequest(
        approval_id="approval-demo-forbidden-sql",
        incident_id=incident_id,
        action="sql.drop_table",
        risk_level="high",
        reason="命中 forbidden destructive SQL 规则，缺少审计上下文。",
        status="rejected",
        step_id="guardrail",
        tool_name="policy_guard",
        requested_by="aiops-agent",
        decided_by="policy-engine",
        decision_reason="forbidden action cannot be approved without change ticket.",
        metadata={"demo_seeded": True, "policy": "forbidden"},
        created_at=_dt(base_time, -160),
        decided_at=_dt(base_time, -159),
    )
    events = [
        _trace_event(
            incident_id,
            trace_id,
            slug,
            1,
            "alert_received",
            "ingest",
            _dt(base_time, -165),
            output_summary="高危 SQL 诊断请求进入",
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            2,
            "plan_created",
            "planner",
            _dt(base_time, -165),
            output_summary="计划优先执行安全边界检查",
            metadata={"plan": plan},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            3,
            "policy_decision",
            "guardrail",
            _dt(base_time, -165),
            step_id="guardrail",
            tool_name="policy_guard",
            latency_ms=80,
            output_summary=evidence[0]["summary"],
            metadata={"decision": "forbidden"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            4,
            "tool_call",
            "executor",
            _dt(base_time, -165),
            step_id="mysql",
            tool_name="inspect_mysql",
            latency_ms=430,
            output_summary=evidence[2]["summary"],
            metadata={"mode": "read_only"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            5,
            "approval_decided",
            "approval",
            _dt(base_time, -165),
            output_summary="策略引擎拒绝审批，要求正式变更流程",
            metadata={"approval_id": approval.approval_id, "decision": "rejected"},
        ),
        _trace_event(
            incident_id,
            trace_id,
            slug,
            6,
            "report_generated",
            "report",
            _dt(base_time, -165),
            output_summary="生成安全阻断报告",
            metadata={"report_id": report_id},
        ),
    ]
    _save_common_records(
        store,
        incident_key="forbidden_sql",
        trace_id=trace_id,
        session_id=session_id,
        report=report,
        plan=plan,
        trace_events=events,
        gathered_evidence=evidence,
        tool_calls=tool_calls,
        final_diagnosis=root_cause,
        remediation=remediation,
        status="blocked",
        status_reason="命中 forbidden 动作规则，已阻断执行",
        base_time=_dt(base_time, -165),
        approval=approval,
    )
    return SeededCase(
        incident_id=incident_id,
        trace_id=trace_id,
        report_id=report_id,
        eval_case_id=DEMO_EVAL_CASE_IDS[incident_id],
        passed=True,
        latency_ms=920,
        metrics=_passing_metrics(approval=True, forbidden=True),
        failed_metrics=[],
        risk_policy="forbidden",
        expected_risk_policy="forbidden",
        expected_needs_approval=True,
        planned_tools=[step["tool_name"] for step in plan if step.get("tool_name")],
        executed_tools=[call["tool_name"] for call in tool_calls],
        forbidden_tools=["execute_sql", "drop_table"],
        confidence=report.confidence,
        evidence_count=len(evidence),
        report_status=report.status,
    )


def _passing_metrics(*, approval: bool, forbidden: bool) -> dict[str, bool | float]:
    metrics: dict[str, bool | float] = {
        "tool_hit": True,
        "tool_sequence_hit": True,
        "executed_tool_hit": True,
        "root_cause_hit": True,
        "risk_policy_hit": True,
        "approval_hit": True,
        "report_generated": True,
        "report_status_hit": True,
        "report_contains_evidence": True,
        "evidence_count_hit": True,
        "confidence_hit": True,
        "runbook_rejection_hit": True,
        "tool_failure_graceful_degradation": True,
        "hypothesis_ranking_hit": True,
        "trace_completeness": True,
        "tool_selection_recall": 1.0,
        "unnecessary_tool_rate": 0.0,
        "evidence_support_rate": 1.0,
        "approval_recall": 1.0 if approval else 0.0,
        "forbidden_precision": 1.0 if forbidden else 0.0,
        "degradation_success": True,
        "forbidden_tools_avoided": True,
    }
    return metrics


def _eval_case(case: SeededCase) -> dict[str, Any]:
    return {
        "id": case.eval_case_id,
        "passed": case.passed,
        "metrics": case.metrics,
        "failed_metrics": case.failed_metrics,
        "failure_reasons": {},
        "planned_tools": case.planned_tools,
        "executed_tools": case.executed_tools,
        "failed_tools": [],
        "forbidden_tools": case.forbidden_tools,
        "risk_policy": case.risk_policy,
        "expected_risk_policy": case.expected_risk_policy,
        "expected_needs_approval": case.expected_needs_approval,
        "approval_required": case.risk_policy in {"approval_required", "forbidden"},
        "report_status": case.report_status,
        "report_id": case.report_id,
        "trace_id": case.trace_id,
        "incident_id": case.incident_id,
        "confidence": case.confidence,
        "evidence_count": case.evidence_count,
        "latency_ms": case.latency_ms,
        "tool_latency_ms": [case.latency_ms],
        "hypothesis_ranking": [{"rank": 1, "confidence": case.confidence}],
        "runbook_rejected": False,
        "runbook_should_reject": False,
    }


def _ratio(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


def _build_eval_summary(cases: list[SeededCase], base_time: datetime) -> dict[str, Any]:
    eval_cases = [_eval_case(case) for case in cases]
    metric_names = sorted({name for case in cases for name in case.metrics})
    metric_summary: dict[str, dict[str, int | float]] = {}
    for name in metric_names:
        values = [case.metrics.get(name) for case in cases]
        passed = sum(1 for value in values if bool(value))
        total = len(values)
        metric_summary[name] = {
            "passed": passed,
            "total": total,
            "pass_rate": _ratio(passed, total),
        }
    latencies = sorted(case.latency_ms for case in cases)
    p95_index = max(0, int(round((len(latencies) - 1) * 0.95)))
    passed_count = sum(1 for case in cases if case.passed)
    categories = {
        "diagnosis": ["root_cause_hit", "hypothesis_ranking_hit", "evidence_support_rate"],
        "tool": ["tool_hit", "tool_sequence_hit", "executed_tool_hit", "unnecessary_tool_rate"],
        "risk": ["risk_policy_hit", "approval_hit", "forbidden_tools_avoided"],
        "stability": ["tool_failure_graceful_degradation", "degradation_success"],
        "diagnostic_chain": ["trace_completeness", "report_contains_evidence"],
    }
    resume_metrics = {
        "case_pass_rate": _ratio(passed_count, len(cases)),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
        "p95_latency_ms": latencies[p95_index],
        "approval_case_count": sum(1 for case in cases if case.expected_needs_approval),
        "forbidden_case_count": sum(1 for case in cases if case.risk_policy == "forbidden"),
    }
    return {
        "generated_at": base_time.isoformat(),
        "source": "seed_demo_data",
        "case_count": len(cases),
        "passed_count": passed_count,
        "pass_rate": _ratio(passed_count, len(cases)),
        "overall_case_count": len(cases),
        "overall_passed_count": passed_count,
        "overall_pass_rate": _ratio(passed_count, len(cases)),
        "all_passed": passed_count == len(cases),
        "failed_cases": [case.eval_case_id for case in cases if not case.passed],
        "metrics": metric_summary,
        "categories": categories,
        "resume_metrics": resume_metrics,
        "p95_latency_ms": latencies[p95_index],
        "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
        "rag_case_count": 0,
        "rag_passed_count": 0,
        "cases": eval_cases,
    }


def _write_eval_markdown(path: Path, summary: dict[str, Any]) -> None:
    md_path = path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AIOps Demo Evaluation Summary",
        "",
        f"- Generated at: {summary['generated_at']}",
        f"- Cases: {summary['passed_count']}/{summary['case_count']} passed",
        f"- Pass rate: {summary['pass_rate']:.0%}",
        f"- Avg latency: {summary['avg_latency_ms']} ms",
        "",
        "| Case | Status | Risk Policy | Evidence | Confidence |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for case in summary["cases"]:
        lines.append(
            "| {id} | {status} | {risk} | {evidence} | {confidence:.2f} |".format(
                id=case["id"],
                status="passed" if case["passed"] else "failed",
                risk=case["risk_policy"],
                evidence=case["evidence_count"],
                confidence=case["confidence"],
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_adapter_summary(base_time: datetime) -> dict[str, Any]:
    checks = [
        {
            "tool_name": "query_metrics",
            "data_source": "prometheus",
            "status": "passed",
            "summary": "演示数据包含 Redis、MySQL 与 K8s 指标证据。",
            "latency_ms": 420,
        },
        {
            "tool_name": "search_logs",
            "data_source": "logs",
            "status": "passed",
            "summary": "演示数据包含服务日志与容器日志证据。",
            "latency_ms": 610,
        },
        {
            "tool_name": "inspect_kubernetes",
            "data_source": "kubernetes",
            "status": "passed",
            "summary": "演示数据包含 Pod 重启、OOMKilled 与 Events。",
            "latency_ms": 470,
        },
        {
            "tool_name": "inspect_redis",
            "data_source": "redis",
            "status": "passed",
            "summary": "演示数据包含 maxclients、连接数与 rejected_connections。",
            "latency_ms": 360,
        },
        {
            "tool_name": "inspect_mysql",
            "data_source": "mysql",
            "status": "passed",
            "summary": "演示数据包含慢查询、执行计划与只读安全检查。",
            "latency_ms": 690,
        },
    ]
    return {
        "generated_at": base_time.isoformat(),
        "source": "seed_demo_data",
        "available": True,
        "status": "passed",
        "summary": "本地面试演示数据已就绪，无需真实外部 Prometheus/K8s/Redis/MySQL。",
        "checks": checks,
        "data_sources": sorted({check["data_source"] for check in checks}),
        "failed_tools": [],
        "duration_ms": sum(check["latency_ms"] for check in checks),
    }


def _print_summary(result: dict[str, Any]) -> None:
    print(f"Seeded {result['incident_count']} demo incidents into {result['database']}")
    print(f"Wrote evaluation summary: {result['eval_summary']}")
    print(f"Wrote adapter summary: {result['adapter_summary']}")
    print("Try: make demo")


def seed_demo_data(
    *,
    database_path: Path | str | None = None,
    backend: str | None = None,
    eval_summary_path: Path | str = DEFAULT_EVAL_SUMMARY,
    adapter_summary_path: Path | str = DEFAULT_ADAPTER_SUMMARY,
    reset: bool = True,
) -> dict[str, Any]:
    eval_summary = Path(eval_summary_path)
    adapter_summary = Path(adapter_summary_path)
    if database_path is not None:
        database = Path(database_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        store = create_aiops_store(database)
        store_label = str(database)
    else:
        selected_backend = (backend or config.aiops_storage_backend or "sqlite").strip().lower()
        store = create_aiops_store(backend=selected_backend)
        store_label = (
            getattr(store, "storage_path", None)
            or getattr(store, "database_path", None)
            or selected_backend
        )
    deleted = store.reset_runtime_data() if reset else {}
    base_time = utc_now().replace(microsecond=0)
    cases = [
        _seed_redis(store, base_time),
        _seed_mysql(store, base_time),
        _seed_k8s(store, base_time),
        _seed_forbidden_sql(store, base_time),
    ]
    summary = _build_eval_summary(cases, base_time)
    adapter = _build_adapter_summary(base_time)
    _write_json(eval_summary, summary)
    _write_eval_markdown(eval_summary, summary)
    _write_json(adapter_summary, adapter)
    status_counts = Counter(case.report_status for case in cases)
    return {
        "database": str(store_label),
        "backend": "sqlite"
        if database_path is not None
        else backend or config.aiops_storage_backend,
        "reset": reset,
        "deleted": deleted,
        "eval_summary": str(eval_summary),
        "adapter_summary": str(adapter_summary),
        "incident_count": len(cases),
        "case_ids": [case.eval_case_id for case in cases],
        "status_counts": dict(status_counts),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed deterministic AIOps demo data.")
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Explicit SQLite database path. Omit to use the configured AIOps backend.",
    )
    parser.add_argument(
        "--backend",
        choices=("sqlite", "mysql"),
        default=config.aiops_storage_backend,
        help="Configured backend used when --database is omitted.",
    )
    parser.add_argument(
        "--eval-summary",
        type=Path,
        default=DEFAULT_EVAL_SUMMARY,
        help="Path for the evaluation summary JSON consumed by the workbench.",
    )
    parser.add_argument(
        "--adapter-summary",
        type=Path,
        default=DEFAULT_ADAPTER_SUMMARY,
        help="Path for the full-stack adapter verification JSON.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Upsert the four demo incidents without clearing existing runtime records.",
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print the human summary.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = seed_demo_data(
        database_path=args.database,
        backend=args.backend,
        eval_summary_path=args.eval_summary,
        adapter_summary_path=args.adapter_summary,
        reset=not args.no_reset,
    )
    if not args.quiet:
        _print_summary(result)


if __name__ == "__main__":
    main()
