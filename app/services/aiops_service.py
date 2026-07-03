"""
通用 Plan-Execute-Replan 服务
基于 LangGraph 官方教程实现
"""

from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from loguru import logger

from app.agent.aiops import (
    PlanExecuteState,
    create_initial_aiops_state,
    executor,
    planner,
    replanner,
)
from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.approval import ApprovalRequest
from app.models.incident import Incident
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.aiops_event_formatters import (
    format_executor_event,
    format_planner_event,
    format_replanner_event,
)
from app.services.aiops_prompt_builder import (
    build_incident_diagnosis_input,
    format_raw_alert_for_prompt,
)
from app.services.aiops_snapshot_service import save_session_snapshot
from app.services.aiops_store import create_aiops_store
from app.services.incident_lifecycle import (
    incident_status_from_runtime_status,
    infer_terminal_report_status,
    snapshot_status_from_event,
    terminal_event_status,
)
from app.services.report_generator import report_generator
from app.services.trace_service import trace_service

NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"

ADDITIVE_STATE_FIELDS = {
    "past_steps",
    "executed_steps",
    "tool_call_records",
    "gathered_evidence",
    "errors",
    "warnings",
}


class AIOpsService:
    """通用 Plan-Execute-RePlan 服务"""

    def __init__(self):
        """初始化服务"""
        self.checkpointer = MemorySaver()
        self.state_store = create_aiops_store()

        self.graph = self._build_graph()
        logger.info("Plan-Execute-Replan Service 初始化完成")

    def _build_graph(self):
        """构建 Plan-Execute-Replan 工作流"""
        logger.info("构建工作流图...")

        workflow = StateGraph(PlanExecuteState)

        workflow.add_node(NODE_PLANNER, planner)  # 制定计划
        workflow.add_node(NODE_EXECUTOR, executor)  # 执行步骤
        workflow.add_node(NODE_REPLANNER, replanner)  # 重新规划

        workflow.set_entry_point(NODE_PLANNER)

        workflow.add_edge(NODE_PLANNER, NODE_EXECUTOR)  # planner -> executor
        workflow.add_edge(NODE_EXECUTOR, NODE_REPLANNER)  # executor -> replanner

        def should_continue(state: PlanExecuteState) -> str:
            """判断是否继续执行"""
            if state.get("response"):
                logger.info("已生成最终响应，结束流程")
                return END

            if state.get("pending_approval"):
                logger.info("存在待审批动作，暂停自动执行流程")
                return END

            plan = state.get("current_plan") or state.get("plan", [])
            if plan:
                logger.info(f"继续执行，剩余 {len(plan)} 个步骤")
                return NODE_EXECUTOR

            logger.info("计划执行完毕，生成最终响应")
            return END

        workflow.add_conditional_edges(
            NODE_REPLANNER, should_continue, {NODE_EXECUTOR: NODE_EXECUTOR, END: END}
        )

        compiled_graph = workflow.compile(checkpointer=self.checkpointer)

        logger.info("工作流图构建完成")
        return compiled_graph

    async def execute(
        self,
        user_input: str,
        session_id: str | None = None,
        incident: Incident | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        执行 Plan-Execute-Replan 流程

        Args:
            user_input: 用户的任务描述
            session_id: 会话ID

        Yields:
            Dict[str, Any]: 流式事件
        """
        session_id = session_id or f"session-{uuid4().hex}"
        logger.info(f"[会话 {session_id}] 开始执行任务: {user_input}")

        try:
            initial_state = create_initial_aiops_state(
                user_input=user_input,
                session_id=session_id,
                incident=incident,
            )
            trace_id = initial_state["trace_id"]
            incident_id = _extract_incident_id(dict(initial_state))
            trace_service.create_event(
                trace_id=trace_id,
                incident_id=incident_id,
                node_name="workflow",
                event_type="workflow_started",
                input_summary=user_input,
                output_summary="AIOps workflow started",
                status="success",
                metadata={"session_id": session_id},
            )
            self._save_session_snapshot(
                session_id=session_id,
                state=dict(initial_state),
                status="running",
                node_name="workflow",
            )

            config_dict = {"configurable": {"thread_id": session_id}}

            async for event in self.graph.astream(
                input=initial_state, config=config_dict, stream_mode="updates"
            ):
                for node_name, node_output in event.items():
                    logger.info(f"节点 '{node_name}' 输出事件")

                    if node_name == NODE_PLANNER:
                        event_payload = self._format_planner_event(node_output)

                    elif node_name == NODE_EXECUTOR:
                        event_payload = self._format_executor_event(node_output)

                    elif node_name == NODE_REPLANNER:
                        event_payload = self._format_replanner_event(node_output)

                    else:
                        event_payload = {
                            "type": "status",
                            "stage": node_name,
                            "message": f"节点 {node_name} 已执行",
                        }

                    trace_event = trace_service.record_node_event(
                        trace_id=trace_id,
                        incident_id=incident_id,
                        node_name=node_name,
                        node_output=node_output if isinstance(node_output, dict) else {},
                        metadata={"sse_type": event_payload.get("type", "")},
                    )
                    snapshot_state = self.get_checkpoint_values(session_id)
                    if isinstance(node_output, dict):
                        snapshot_state = _merge_checkpoint_with_node_output(
                            snapshot_state,
                            node_output,
                        )
                    self._save_session_snapshot(
                        session_id=session_id,
                        state=snapshot_state,
                        status=_snapshot_status_from_event(event_payload),
                        node_name=node_name,
                    )
                    yield _attach_trace_event(event_payload, trace_event)

            final_state = self.graph.get_state(config_dict)
            final_response = ""

            final_values = final_state.values if final_state and final_state.values else {}

            if final_values:
                final_response = final_values.get("response", "")

            incident_state = final_values.get("incident", {}) if final_values else {}
            final_incident_id = (
                incident_state.get("incident_id", "")
                if isinstance(incident_state, dict)
                else getattr(incident_state, "incident_id", "")
            )
            incident_id = final_incident_id or incident_id
            trace_id = (final_values.get("trace_id") if final_values else None) or trace_id
            pending_approval = final_values.get("pending_approval") if final_values else None
            risk_assessment = final_values.get("risk_assessment") if final_values else None
            structured_report = final_values.get("report") if final_values else None
            if not final_response:
                final_response = _build_fallback_final_response(final_values)
            if not structured_report and final_values:
                generated_report = report_generator.generate_from_state(
                    final_values,
                    status=_infer_terminal_report_status(final_values),
                )
                structured_report = generated_report.model_dump(mode="json")
                final_response = generated_report.markdown
            terminal_status = _terminal_event_status(
                {
                    "structured_report": structured_report,
                    "pending_approval": pending_approval,
                    "risk_assessment": risk_assessment,
                    "errors": final_values.get("errors") if final_values else [],
                    "warnings": final_values.get("warnings") if final_values else [],
                }
            )
            final_snapshot_state = dict(final_values or {})
            final_snapshot_state["response"] = final_response
            final_snapshot_state["report"] = structured_report
            final_snapshot_state["pending_approval"] = pending_approval
            final_snapshot_state["risk_assessment"] = risk_assessment
            self._save_session_snapshot(
                session_id=session_id,
                state=final_snapshot_state,
                status=terminal_status,
                node_name="workflow",
            )

            complete_trace_event = trace_service.create_event(
                trace_id=trace_id,
                incident_id=incident_id,
                node_name="workflow",
                event_type="workflow_completed",
                output_summary="AIOps workflow completed",
                status=terminal_status,
                metadata={"session_id": session_id},
            )
            yield {
                "type": "complete",
                "stage": "complete",
                "status": terminal_status,
                "message": "任务执行完成",
                "response": final_response,
                "incident_id": incident_id,
                "trace_id": trace_id,
                "trace_event_id": complete_trace_event.event_id,
                "trace_event": complete_trace_event.model_dump(mode="json"),
                "pending_approval": pending_approval,
                "risk_assessment": risk_assessment,
                "structured_report": structured_report,
            }

            logger.info(f"[会话 {session_id}] 任务执行完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] 任务执行失败: {e}", exc_info=True)
            self._save_session_snapshot(
                session_id=session_id,
                state=dict(locals().get("initial_state", {}) or {}),
                status="failed",
                node_name="workflow",
            )
            error_trace_event = trace_service.create_event(
                trace_id=locals().get("trace_id", "trace-unknown"),
                incident_id=locals().get("incident_id", "incident-unknown"),
                node_name="workflow",
                event_type="workflow_error",
                output_summary=str(e),
                status="failed",
                error_message=str(e),
                metadata={"session_id": session_id},
            )
            yield {
                "type": "error",
                "stage": "error",
                "status": "failed",
                "message": f"任务执行出错: {str(e)}",
                "trace_id": error_trace_event.trace_id,
                "trace_event_id": error_trace_event.event_id,
                "trace_event": error_trace_event.model_dump(mode="json"),
            }

    async def diagnose(
        self,
        session_id: str | None = None,
        incident: Incident | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        AIOps 诊断接口（兼容旧接口）

        Args:
            session_id: 会话ID

        Yields:
            Dict[str, Any]: 诊断过程的流式事件
        """
        from textwrap import dedent

        session_id = session_id or f"session-{uuid4().hex}"
        aiops_task = dedent(
            """诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告，诊断报告输出格式要求：
                ```
                # 告警分析报告

                ---

                ## 📋 活跃告警清单

                | 告警名称 | 级别 | 目标服务 | 首次触发时间 | 最新触发时间 | 状态 |
                |---------|------|----------|-------------|-------------|------|
                | [告警1名称] | [级别] | [服务名] | [时间] | [时间] | 活跃 |
                | [告警2名称] | [级别] | [服务名] | [时间] | [时间] | 活跃 |

                ---

                ## 🔍 告警根因分析1 - [告警名称]

                ### 告警详情
                - **告警级别**: [级别]
                - **受影响服务**: [服务名]
                - **持续时间**: [X分钟]

                ### 症状描述
                [根据监控指标描述症状]

                ### 日志证据
                [引用查询到的关键日志]

                ### 根因结论
                [基于证据得出的根本原因]

                ---

                ## 🛠️ 处理方案执行1 - [告警名称]

                ### 已执行的排查步骤
                1. [步骤1]
                2. [步骤2]

                ### 处理建议
                [给出具体的处理建议]

                ### 预期效果
                [说明预期的效果]

                ---

                ## 🔍 告警根因分析2 - [告警名称]
                [如果有第2个告警，重复上述格式]

                ---

                ## 📊 结论

                ### 整体评估
                [总结所有告警的整体情况]

                ### 关键发现
                - [发现1]
                - [发现2]

                ### 后续建议
                1. [建议1]
                2. [建议2]

                ### 风险评估
                [评估当前风险等级和影响范围]
                ```

                **重要提醒**：
                - 最终输出必须是纯 Markdown 文本，不要包含 JSON 结构
                - 所有内容必须基于工具查询的真实数据，严禁编造
                - 如果某个步骤失败，在结论中如实说明，不要跳过"""
        )

        diagnosis_input = _build_incident_diagnosis_input(aiops_task, incident)

        async for event in self.execute(diagnosis_input, session_id, incident=incident):
            if event.get("type") == "complete":
                diagnosis_status = _terminal_event_status(event)
                # 将 response 包装为 diagnosis 格式
                yield {
                    "type": "complete",
                    "stage": "diagnosis_complete",
                    "status": diagnosis_status,
                    "message": "诊断流程完成",
                    "incident_id": event.get("incident_id", ""),
                    "trace_id": event.get("trace_id", ""),
                    "pending_approval": event.get("pending_approval"),
                    "risk_assessment": event.get("risk_assessment"),
                    "structured_report": event.get("structured_report"),
                    "diagnosis": {
                        "status": diagnosis_status,
                        "report": event.get("response", ""),
                        "structured_report": event.get("structured_report"),
                    },
                }
            else:
                yield event

    def get_checkpoint_values(self, session_id: str) -> dict[str, Any]:
        """Return the current in-memory graph checkpoint values for a session."""
        config_dict = {"configurable": {"thread_id": session_id}}
        snapshot = self.graph.get_state(config_dict)
        if not snapshot or not snapshot.values:
            return {}
        return dict(snapshot.values)

    def get_session_snapshot(self, session_id: str) -> AIOpsSessionSnapshot | None:
        """Return the latest durable AIOps session snapshot."""
        return cast(
            AIOpsSessionSnapshot | None,
            self.state_store.get_aiops_session_snapshot(session_id),
        )

    def list_session_snapshots(
        self,
        *,
        incident_id: str | None = None,
        limit: int = 20,
    ) -> list[AIOpsSessionSnapshot]:
        """Return recent durable AIOps session snapshots."""
        return cast(
            list[AIOpsSessionSnapshot],
            self.state_store.list_aiops_session_snapshots(
                incident_id=incident_id,
                limit=limit,
            ),
        )

    def _load_resume_session_snapshot(
        self,
        *,
        session_id: str,
        incident_id: str,
    ) -> AIOpsSessionSnapshot | None:
        """Find a persisted session snapshot suitable for approval resume."""
        snapshot = cast(
            AIOpsSessionSnapshot | None,
            self.state_store.get_aiops_session_snapshot(session_id),
        )
        if snapshot is not None:
            if snapshot.incident_id != incident_id:
                raise ValueError("session_id does not belong to the requested incident")
            return snapshot
        return cast(
            AIOpsSessionSnapshot | None,
            self.state_store.get_latest_aiops_session_snapshot(incident_id),
        )

    async def resume_after_approval(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Close the diagnosis loop after a human approval decision.

        The approval confirms the human change plan, but this endpoint does not execute
        production mutations. It resumes the diagnostic lifecycle by persisting a fresh
        post-approval report and trace event from the paused checkpoint.
        """
        if approval.incident_id != incident_id:
            raise ValueError("approval_id does not belong to the requested incident")
        if approval.status != "approved":
            raise ValueError("diagnosis can only resume after an approved decision")

        values = self.get_checkpoint_values(session_id)
        resume_source = "checkpoint"
        persisted_report: DiagnosisReport | None = None
        if values:
            checkpoint_incident_id = _extract_incident_id(values)
            if checkpoint_incident_id != incident_id:
                raise ValueError("session_id does not belong to the requested incident")
            trace_id = str(
                values.get("trace_id") or approval.metadata.get("trace_id") or "trace-unknown"
            )
        else:
            snapshot = self._load_resume_session_snapshot(
                session_id=session_id,
                incident_id=incident_id,
            )
            if snapshot is not None:
                session_id = snapshot.session_id
                values = snapshot.to_state()
                trace_id = snapshot.trace_id or str(
                    approval.metadata.get("trace_id") or "trace-unknown"
                )
                resume_source = "session_snapshot"
            else:
                persisted_report = report_generator.get_report(incident_id)
                if persisted_report is None:
                    raise LookupError(
                        "No paused checkpoint, session snapshot, or persisted report "
                        f"for incident {incident_id}"
                    )
                trace_id = persisted_report.trace_id or str(
                    approval.metadata.get("trace_id") or "trace-unknown"
                )
                resume_source = "report_fallback"

        resume_event = trace_service.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="workflow",
            event_type="diagnosis_resumed",
            input_summary=f"approval_id={approval.approval_id}",
            output_summary="Approved human decision recorded; agent will not execute production change",
            status="success",
            metadata={
                "session_id": session_id,
                "approval_id": approval.approval_id,
                "approval_status": approval.status,
                "resume_source": resume_source,
                "boundary": "agent_does_not_execute_production_change",
            },
        )
        yield {
            "type": "status",
            "stage": "diagnosis_resumed",
            "status": "running",
            "message": "审批已通过，正在记录人工决策并更新诊断报告；Agent 不会自动执行生产变更",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "trace_event_id": resume_event.event_id,
            "trace_event": resume_event.model_dump(mode="json"),
            "resume_source": resume_source,
            "execution_boundary": "agent_does_not_execute_production_change",
        }

        approval_payload = approval.model_dump(mode="json")
        if values:
            report_state = dict(values)
            risk_summary = dict(report_state.get("risk_assessment") or {})
            risk_summary["approval_decision"] = approval_payload
            report_state["pending_approval"] = approval_payload
            report_state["risk_assessment"] = risk_summary
            report_state["response"] = ""
            report = report_generator.generate_from_state(
                report_state,
                status="approval_resumed",
            )

            if resume_source == "checkpoint":
                self._best_effort_update_checkpoint_after_resume(
                    session_id=session_id,
                    report=report.model_dump(mode="json"),
                )
            resumed_state = dict(values)
            resumed_state["pending_approval"] = None
            resumed_state["risk_assessment"] = risk_summary
            resumed_state["report"] = report.model_dump(mode="json")
            resumed_state["response"] = report.markdown
            self._save_session_snapshot(
                session_id=session_id,
                state=resumed_state,
                status="approval_resumed",
                node_name="workflow",
            )
        else:
            report = _build_persisted_resume_report(
                persisted_report=persisted_report,
                approval=approval,
                session_id=session_id,
            )
            risk_summary = dict(report.risk_summary or {})
            report_generator.save_report(report)
            self._save_session_snapshot(
                session_id=session_id,
                state={
                    "session_id": session_id,
                    "input": report.summary,
                    "trace_id": trace_id,
                    "incident": {
                        "incident_id": incident_id,
                        "title": report.title,
                        "service_name": report.service_name,
                        "severity": report.severity,
                        "environment": report.environment,
                    },
                    "risk_assessment": risk_summary,
                    "pending_approval": None,
                    "report": report.model_dump(mode="json"),
                    "response": report.markdown,
                },
                status="approval_resumed",
                node_name="workflow",
            )

        report_event = trace_service.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="report_generator",
            event_type="report_resumed",
            input_summary=f"approval_id={approval.approval_id}",
            output_summary=f"report_id={report.report_id}, status={report.status}",
            status=report.status,
            metadata={
                "session_id": session_id,
                "approval_id": approval.approval_id,
                "report_id": report.report_id,
                "resume_source": resume_source,
            },
        )
        yield {
            "type": "report",
            "stage": "resumed_report",
            "status": report.status,
            "message": "审批结果已写入诊断报告，生产动作仍需通过安全变更流程处理",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "trace_event_id": report_event.event_id,
            "trace_event": report_event.model_dump(mode="json"),
            "resume_source": resume_source,
            "execution_boundary": "agent_does_not_execute_production_change",
            "report": report.markdown,
            "structured_report": report.model_dump(mode="json"),
        }

        complete_event = trace_service.create_event(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="workflow",
            event_type="resume_completed",
            input_summary=f"approval_id={approval.approval_id}",
            output_summary="Approval resume lifecycle completed",
            status="success",
            metadata={
                "session_id": session_id,
                "approval_id": approval.approval_id,
                "resume_source": resume_source,
            },
        )
        yield {
            "type": "complete",
            "stage": "resume_complete",
            "status": report.status,
            "message": "审批结果记录完成，诊断闭环已更新；Agent 未执行任何生产变更",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "trace_event_id": complete_event.event_id,
            "trace_event": complete_event.model_dump(mode="json"),
            "resume_source": resume_source,
            "execution_boundary": "agent_does_not_execute_production_change",
            "pending_approval": None,
            "risk_assessment": risk_summary,
            "structured_report": report.model_dump(mode="json"),
            "diagnosis": {
                "status": report.status,
                "report": report.markdown,
                "structured_report": report.model_dump(mode="json"),
            },
        }

    def _best_effort_update_checkpoint_after_resume(
        self,
        *,
        session_id: str,
        report: dict[str, Any],
    ) -> None:
        """Update in-memory checkpoint state after resume without failing the API."""
        config_dict = {"configurable": {"thread_id": session_id}}
        try:
            self.graph.update_state(
                config_dict,
                {
                    "pending_approval": None,
                    "response": report.get("markdown", ""),
                    "report": report,
                },
                as_node=NODE_REPLANNER,
            )
        except Exception as exc:
            logger.warning(f"更新 resume checkpoint 失败，将以持久化报告为准: {exc}")

    def _save_session_snapshot(
        self,
        *,
        session_id: str,
        state: dict[str, Any],
        status: str,
        node_name: str,
    ) -> None:
        """Persist a best-effort durable snapshot for diagnosis recovery."""
        try:
            save_session_snapshot(
                self.state_store,
                session_id=session_id,
                state=state,
                status=status,
                node_name=node_name,
            )
        except Exception as exc:
            logger.warning(f"保存 AIOps session snapshot 失败: {exc}")

    def _format_planner_event(self, state: dict | None) -> dict:
        """格式化 Planner 节点事件"""
        return format_planner_event(state)

    def _format_executor_event(self, state: dict | None) -> dict:
        """格式化 Executor 节点事件"""
        return format_executor_event(state)

    def _format_replanner_event(self, state: dict | None) -> dict:
        """格式化 Replanner 节点事件"""
        return format_replanner_event(state)


aiops_service = AIOpsService()


def _build_persisted_resume_report(
    *,
    persisted_report: DiagnosisReport | None,
    approval: ApprovalRequest,
    session_id: str,
) -> DiagnosisReport:
    """Build an approval-resumed report when the in-memory checkpoint is gone."""
    if persisted_report is None:
        raise LookupError(f"No persisted report for incident {approval.incident_id}")

    approval_payload = approval.model_dump(mode="json")
    risk_summary = dict(persisted_report.risk_summary or {})
    risk_summary["approval_decision"] = approval_payload
    approval_decision = dict(persisted_report.approval_decision or {})
    approval_decision.update(
        {
            "approval_id": approval.approval_id,
            "action": approval.action,
            "risk_level": approval.risk_level,
            "status": approval.status,
            "reason": approval.reason,
            "tool_name": approval.tool_name,
            "requested_by": approval.requested_by,
            "created_at": approval.created_at.isoformat(),
            "decided_by": approval.decided_by,
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
            "decision_reason": approval.decision_reason,
        }
    )
    uncertainties = [
        item
        for item in persisted_report.uncertainties
        if "等待人工审批" not in item and "需要人工审批" not in item
    ]
    uncertainties.append(
        "审批已通过；本次恢复使用持久化报告补齐 Trace 和报告闭环，后续风险操作需进入安全变更流程。"
    )
    summary = persisted_report.summary
    if "审批已通过" not in summary:
        summary = f"{summary} 审批已通过，已基于持久化报告补齐恢复闭环。"

    markdown = _append_resume_markdown(
        persisted_report.markdown,
        approval=approval,
        session_id=session_id,
    )
    return persisted_report.model_copy(
        update={
            "status": "approval_resumed",
            "approval_status": "approved",
            "approval_decision": approval_decision,
            "risk_summary": risk_summary,
            "manual_action_required": True,
            "summary": summary,
            "uncertainties": list(dict.fromkeys(uncertainties))[:8],
            "markdown": markdown,
        }
    )


def _append_resume_markdown(
    markdown: str,
    *,
    approval: ApprovalRequest,
    session_id: str,
) -> str:
    """Append a stable resume audit section to an existing report markdown."""
    base = markdown.strip() or f"# {approval.incident_id} AIOps 诊断报告"
    section = "\n".join(
        [
            "",
            "## 审批恢复记录",
            f"- 审批ID：{approval.approval_id}",
            f"- 审批状态：{approval.status}",
            f"- 审批人：{approval.decided_by or '未记录'}",
            f"- 审批原因：{approval.decision_reason or approval.reason or '未填写'}",
            f"- 恢复 session：{session_id}",
            "- 恢复边界：使用持久化报告补齐 Trace 和报告闭环；"
            "Agent 不直接执行生产写操作，后续风险操作需进入安全变更流程。",
        ]
    )
    if "## 审批恢复记录" in base:
        return base
    return f"{base}\n{section}"


def _extract_incident_id(state: dict[str, Any]) -> str:
    """Extract incident_id from a LangGraph state snapshot."""
    incident = state.get("incident") or {}
    if isinstance(incident, dict):
        return str(incident.get("incident_id") or "incident-unknown")
    return str(getattr(incident, "incident_id", "incident-unknown"))


def _attach_trace_event(event_payload: dict[str, Any], trace_event: TraceEvent) -> dict[str, Any]:
    """Add trace metadata to an SSE event without changing its original shape."""
    event_payload["trace_id"] = trace_event.trace_id
    event_payload["trace_event_id"] = trace_event.event_id
    event_payload["trace_event"] = trace_event.model_dump(mode="json")
    return event_payload


def _build_fallback_final_response(state: dict[str, Any]) -> str:
    """Build a non-empty final response when the graph ends without response."""
    report = state.get("report") or {}
    if isinstance(report, dict) and report.get("markdown"):
        return str(report["markdown"])

    incident = state.get("incident") or {}
    if isinstance(incident, dict):
        incident_id = incident.get("incident_id") or "unknown"
        service_name = incident.get("service_name") or "unknown-service"
        symptom = incident.get("symptom") or state.get("input") or "未提供故障现象"
    else:
        incident_id = getattr(incident, "incident_id", "unknown")
        service_name = getattr(incident, "service_name", "unknown-service")
        symptom = getattr(incident, "symptom", None) or state.get("input") or "未提供故障现象"

    pending_approval = state.get("pending_approval")
    past_steps = state.get("past_steps") or []
    errors = state.get("errors") or []
    warnings = state.get("warnings") or []

    if pending_approval:
        return (
            "# AIOps 诊断已暂停，等待人工审批\n\n"
            f"- 事件：{incident_id}\n"
            f"- 服务：{service_name}\n"
            f"- 现象：{symptom}\n"
            f"- 已执行步骤数：{len(past_steps)}\n"
            "- 状态：检测到需要人工审批的动作，自动执行已暂停。\n"
        )

    error_block = ""
    if errors:
        error_preview = "; ".join(str(error) for error in errors[:3])
        error_block = f"\n- 已记录错误：{error_preview}\n"
    warning_block = ""
    if warnings:
        warning_preview = "; ".join(str(warning) for warning in warnings[:3])
        warning_block = f"\n- 已记录运行告警：{warning_preview}\n"

    return (
        "# AIOps 诊断流程已结束\n\n"
        f"- 事件：{incident_id}\n"
        f"- 服务：{service_name}\n"
        f"- 现象：{symptom}\n"
        f"- 已执行步骤数：{len(past_steps)}\n"
        "- 状态：流程结束时未生成最终诊断报告，请结合 Trace 和已采集证据继续排查。\n"
        f"{error_block}"
        f"{warning_block}"
    )


def _infer_terminal_report_status(state: dict[str, Any]) -> str:
    """Infer a report status for graph terminal states that missed Replanner finalization."""
    return infer_terminal_report_status(state)


def _snapshot_status_from_event(event: dict[str, Any]) -> str:
    """Map streamed workflow events to durable session snapshot states."""
    return snapshot_status_from_event(event)


def _incident_status_from_runtime_status(status: str) -> str:
    """Normalize runtime/report statuses into incident lifecycle statuses."""
    return incident_status_from_runtime_status(status)


def _terminal_event_status(event: dict[str, Any]) -> str:
    """Derive the legacy terminal status from the structured report contract."""
    return terminal_event_status(event)


def _merge_checkpoint_with_node_output(
    checkpoint_state: dict[str, Any],
    node_output: dict[str, Any],
) -> dict[str, Any]:
    """Merge LangGraph node deltas into a durable snapshot without losing additive fields."""
    merged = dict(checkpoint_state or {})
    for key, value in node_output.items():
        if key not in ADDITIVE_STATE_FIELDS or not isinstance(value, list):
            merged[key] = value
            continue

        existing = merged.get(key)
        if not isinstance(existing, list):
            merged[key] = value
        elif _list_endswith(existing, value):
            merged[key] = existing
        else:
            merged[key] = [*existing, *value]
    return merged


def _list_endswith(values: list[Any], suffix: list[Any]) -> bool:
    if not suffix:
        return True
    if len(suffix) > len(values):
        return False
    return values[-len(suffix) :] == suffix


def _build_incident_diagnosis_input(base_task: str, incident: Incident | None) -> str:
    """Render the structured incident into the planner-facing diagnosis request."""
    return build_incident_diagnosis_input(base_task, incident)


def _format_raw_alert_for_prompt(raw_alert: dict[str, Any], max_chars: int = 4000) -> str:
    """Serialize raw alert fields for planning while keeping the prompt bounded."""
    return format_raw_alert_for_prompt(raw_alert, max_chars=max_chars)
