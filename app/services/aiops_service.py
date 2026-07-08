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
from app.services.aiops_diagnosis_tasks import DEFAULT_AIOPS_DIAGNOSIS_TASK
from app.services.aiops_event_formatters import (
    format_executor_event,
    format_planner_event,
    format_replanner_event,
)
from app.services.aiops_progress import (
    attach_progress,
    build_progress_from_event,
    build_progress_payload,
    progress_event_payload,
    state_with_progress,
)
from app.services.aiops_resume_reports import _build_persisted_resume_report
from app.services.aiops_service_helpers import (
    _attach_trace_event,
    _build_fallback_final_response,
    _build_incident_diagnosis_input,
    _extract_incident_id,
    _format_raw_alert_for_prompt,
    _incident_status_from_runtime_status,
    _infer_terminal_report_status,
    _merge_checkpoint_with_node_output,
    _snapshot_status_from_event,
    _terminal_event_status,
)
from app.services.aiops_snapshot_service import save_session_snapshot
from app.services.aiops_store import create_aiops_store
from app.services.report_generator import report_generator
from app.services.trace_service import trace_service
from app.utils.log_safety import summarize_text_for_log
from app.utils.public_errors import GENERIC_DIAGNOSIS_ERROR, public_exception_message

NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"

__all__ = [
    "AIOpsService",
    "aiops_service",
    "_attach_trace_event",
    "_build_fallback_final_response",
    "_build_incident_diagnosis_input",
    "_extract_incident_id",
    "_format_raw_alert_for_prompt",
    "_incident_status_from_runtime_status",
    "_infer_terminal_report_status",
    "_merge_checkpoint_with_node_output",
    "_snapshot_status_from_event",
    "_terminal_event_status",
]


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
        progress_index = 0

        def next_progress_cursor() -> str:
            nonlocal progress_index
            progress_index += 1
            return f"{session_id}:{progress_index:06d}"

        logger.info(
            f"[会话 {session_id}] 开始执行任务: "
            f"{summarize_text_for_log(user_input, label='aiops_input')}"
        )

        try:
            initial_state = create_initial_aiops_state(
                user_input=user_input,
                session_id=session_id,
                incident=incident,
            )
            trace_id = initial_state["trace_id"]
            incident_id = _extract_incident_id(dict(initial_state))
            start_trace_event = trace_service.create_event(
                trace_id=trace_id,
                incident_id=incident_id,
                node_name="workflow",
                event_type="workflow_started",
                input_summary=user_input,
                output_summary="AIOps workflow started",
                status="success",
                metadata={"session_id": session_id},
            )
            start_progress = build_progress_payload(
                dict(initial_state),
                phase="workflow",
                node_name="workflow",
                cursor=next_progress_cursor(),
                status="running",
                message="AIOps workflow started",
            )
            initial_snapshot_state = state_with_progress(dict(initial_state), start_progress)
            self._save_session_snapshot(
                session_id=session_id,
                state=initial_snapshot_state,
                status="running",
                node_name="workflow",
            )
            yield _attach_trace_event(progress_event_payload(start_progress), start_trace_event)

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
                    progress = build_progress_from_event(
                        event_payload,
                        snapshot_state,
                        node_name=node_name,
                        cursor=next_progress_cursor(),
                    )
                    snapshot_state = state_with_progress(snapshot_state, progress)
                    self._save_session_snapshot(
                        session_id=session_id,
                        state=snapshot_state,
                        status=_snapshot_status_from_event(event_payload),
                        node_name=node_name,
                    )
                    yield _attach_trace_event(progress_event_payload(progress), trace_event)
                    yield _attach_trace_event(attach_progress(event_payload, progress), trace_event)

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
                report_progress = build_progress_payload(
                    dict(final_values),
                    phase="reporting",
                    node_name="report_generator",
                    cursor=next_progress_cursor(),
                    status="running",
                    report_status="generating",
                    message="Generating diagnosis report",
                )
                self._save_session_snapshot(
                    session_id=session_id,
                    state=state_with_progress(dict(final_values), report_progress),
                    status="running",
                    node_name="report_generator",
                )
                yield progress_event_payload(report_progress)
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
            complete_progress = build_progress_payload(
                final_snapshot_state,
                phase="complete",
                node_name="workflow",
                cursor=next_progress_cursor(),
                status=terminal_status,
                report_status=(
                    str((structured_report or {}).get("status") or terminal_status)
                    if isinstance(structured_report, dict)
                    else terminal_status
                ),
                message="AIOps workflow completed",
            )
            final_snapshot_state = state_with_progress(final_snapshot_state, complete_progress)
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
            yield _attach_trace_event(progress_event_payload(complete_progress), complete_trace_event)
            yield attach_progress(
                {
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
                },
                complete_progress,
            )

            logger.info(f"[会话 {session_id}] 任务执行完成")

        except Exception as e:
            logger.error(
                f"[会话 {session_id}] 任务执行失败: "
                f"error_type={type(e).__name__}, {summarize_text_for_log(e, label='error')}"
            )
            public_message = public_exception_message(e, fallback=GENERIC_DIAGNOSIS_ERROR)
            error_state = dict(locals().get("initial_state", {}) or {})
            error_progress = build_progress_payload(
                error_state,
                phase="error",
                node_name="workflow",
                cursor=next_progress_cursor(),
                status="failed",
                report_status="failed",
                message=public_message,
            )
            self._save_session_snapshot(
                session_id=session_id,
                state=state_with_progress(error_state, error_progress),
                status="failed",
                node_name="workflow",
            )
            error_trace_event = trace_service.create_event(
                trace_id=locals().get("trace_id", "trace-unknown"),
                incident_id=locals().get("incident_id", "incident-unknown"),
                node_name="workflow",
                event_type="workflow_error",
                output_summary=public_message,
                status="failed",
                error_message=public_message,
                metadata={"session_id": session_id},
            )
            yield _attach_trace_event(progress_event_payload(error_progress), error_trace_event)
            yield attach_progress(
                {
                "type": "error",
                "stage": "error",
                "status": "failed",
                "message": public_message,
                "trace_id": error_trace_event.trace_id,
                "trace_event_id": error_trace_event.event_id,
                "trace_event": error_trace_event.model_dump(mode="json"),
                },
                error_progress,
            )

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
        session_id = session_id or f"session-{uuid4().hex}"
        diagnosis_input = _build_incident_diagnosis_input(
            DEFAULT_AIOPS_DIAGNOSIS_TASK,
            incident,
        )

        async for event in self.execute(diagnosis_input, session_id, incident=incident):
            if event.get("type") == "complete":
                diagnosis_status = _terminal_event_status(event)
                # 将 response 包装为 diagnosis 格式
                yield {
                    "type": "complete",
                    "stage": "diagnosis_complete",
                    "status": diagnosis_status,
                    "message": "诊断流程完成",
                    "response": event.get("response", ""),
                    "incident_id": event.get("incident_id", ""),
                    "trace_id": event.get("trace_id", ""),
                    "progress": event.get("progress"),
                    "progress_cursor": event.get("progress_cursor", ""),
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
        offset: int = 0,
    ) -> list[AIOpsSessionSnapshot]:
        """Return recent durable AIOps session snapshots."""
        return cast(
            list[AIOpsSessionSnapshot],
            self.state_store.list_aiops_session_snapshots(
                incident_id=incident_id,
                limit=limit,
                offset=offset,
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

        progress_index = 0

        def next_resume_progress_cursor() -> str:
            nonlocal progress_index
            progress_index += 1
            return f"{session_id}:resume-{progress_index:06d}"

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
        resume_state = dict(values or {})
        if not resume_state:
            resume_state = {
                "session_id": session_id,
                "trace_id": trace_id,
                "incident": {"incident_id": incident_id},
            }
        resume_progress = build_progress_payload(
            resume_state,
            phase="approval",
            node_name="workflow",
            cursor=next_resume_progress_cursor(),
            status="running",
            report_status="generating",
            message="Approved decision recorded; resuming diagnosis",
        )
        yield _attach_trace_event(progress_event_payload(resume_progress), resume_event)
        yield attach_progress(
            {
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
            },
            resume_progress,
        )

        approval_payload = approval.model_dump(mode="json")
        resumed_snapshot_state: dict[str, Any]
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
            report_progress = build_progress_payload(
                resumed_state,
                phase="reporting",
                node_name="report_generator",
                cursor=next_resume_progress_cursor(),
                status="approval_resumed",
                report_status=report.status,
                message="Approval resume report generated",
            )
            resumed_snapshot_state = state_with_progress(resumed_state, report_progress)
            self._save_session_snapshot(
                session_id=session_id,
                state=resumed_snapshot_state,
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
            resumed_state = {
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
            }
            report_progress = build_progress_payload(
                resumed_state,
                phase="reporting",
                node_name="report_generator",
                cursor=next_resume_progress_cursor(),
                status="approval_resumed",
                report_status=report.status,
                message="Approval resume report generated",
            )
            resumed_snapshot_state = state_with_progress(resumed_state, report_progress)
            self._save_session_snapshot(
                session_id=session_id,
                state=resumed_snapshot_state,
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
        yield _attach_trace_event(progress_event_payload(report_progress), report_event)
        yield attach_progress(
            {
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
            },
            report_progress,
        )

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
        complete_progress = build_progress_payload(
            resumed_snapshot_state,
            phase="complete",
            node_name="workflow",
            cursor=next_resume_progress_cursor(),
            status=report.status,
            report_status=report.status,
            message="Approval resume lifecycle completed",
        )
        self._save_session_snapshot(
            session_id=session_id,
            state=state_with_progress(resumed_snapshot_state, complete_progress),
            status=report.status,
            node_name="workflow",
        )
        yield _attach_trace_event(progress_event_payload(complete_progress), complete_event)
        yield attach_progress(
            {
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
            },
            complete_progress,
        )

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
