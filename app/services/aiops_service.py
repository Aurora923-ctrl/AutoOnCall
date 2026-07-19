"""
通用 Plan-Execute-Replan 服务
基于 LangGraph 官方教程实现
"""

import asyncio
from collections.abc import AsyncGenerator
from threading import Lock
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
from app.services.aiops_snapshot_service import (
    create_session_snapshot,
    save_session_snapshot,
    transition_session_snapshot,
)
from app.services.aiops_store import create_aiops_store
from app.services.approval_service import ApprovalService
from app.services.report_generator import report_generator
from app.services.trace_service import trace_service
from app.utils.log_safety import summarize_text_for_log
from app.utils.public_errors import GENERIC_DIAGNOSIS_ERROR, public_exception_message

NODE_PLANNER = "planner"
NODE_EXECUTOR = "executor"
NODE_REPLANNER = "replanner"


class AIOpsRunConflictError(ValueError):
    """Raised when a diagnosis run identity is already owned."""


class AIOpsResumeConflictError(ValueError):
    """Raised when an approval resume was already claimed or completed."""


__all__ = [
    "AIOpsResumeConflictError",
    "AIOpsRunConflictError",
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
        self._active_run_lock = Lock()
        self._active_diagnosis_sessions: set[str] = set()
        self._active_resume_approvals: set[str] = set()

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
        run_claimed = False
        terminal_persisted = False

        def next_progress_cursor() -> str:
            nonlocal progress_index
            progress_index += 1
            return f"{session_id}:{progress_index:06d}"

        logger.info(
            f"[会话 {session_id}] 开始执行任务: "
            f"{summarize_text_for_log(user_input, label='aiops_input')}"
        )

        self._claim_diagnosis_session(session_id)
        run_claimed = True
        try:
            if self.state_store.get_aiops_session_snapshot(session_id) is not None:
                raise AIOpsRunConflictError(
                    "session_id already belongs to an existing diagnosis run"
                )
            initial_state = create_initial_aiops_state(
                user_input=user_input,
                session_id=session_id,
                incident=incident,
            )
            trace_id = initial_state["trace_id"]
            incident_id = _extract_incident_id(dict(initial_state))
            start_progress = build_progress_payload(
                dict(initial_state),
                phase="workflow",
                node_name="workflow",
                cursor=next_progress_cursor(),
                status="running",
                message="AIOps workflow started",
            )
            initial_snapshot_state = state_with_progress(dict(initial_state), start_progress)
            if not create_session_snapshot(
                self.state_store,
                session_id=session_id,
                state=initial_snapshot_state,
                status="running",
                node_name="workflow",
            ):
                raise AIOpsRunConflictError(
                    "session_id already belongs to an existing diagnosis run"
                )
            start_trace_event = None
            try:
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
            except Exception as trace_exc:
                logger.warning(f"记录 AIOps 启动 Trace 失败: {trace_exc}")
            start_payload = progress_event_payload(start_progress)
            if start_trace_event is not None:
                start_payload = _attach_trace_event(start_payload, start_trace_event)
            yield start_payload

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

                    trace_event = None
                    try:
                        trace_event = trace_service.record_node_event(
                            trace_id=trace_id,
                            incident_id=incident_id,
                            node_name=node_name,
                            node_output=node_output if isinstance(node_output, dict) else {},
                            metadata={"sse_type": event_payload.get("type", "")},
                        )
                    except Exception as trace_exc:
                        logger.warning(f"记录 AIOps 节点 Trace 失败: {trace_exc}")
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
                    progress_payload = progress_event_payload(progress)
                    business_payload = attach_progress(event_payload, progress)
                    if trace_event is not None:
                        progress_payload = _attach_trace_event(progress_payload, trace_event)
                        business_payload = _attach_trace_event(business_payload, trace_event)
                    yield progress_payload
                    yield business_payload

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
            structured_report = dict(structured_report or {})
            structured_report["degradation_analysis"] = structured_report.get(
                "degradation_analysis"
            ) or (
                (final_values or {}).get("evidence_analysis", {}).get("degradation_analysis", {})
                if isinstance((final_values or {}).get("evidence_analysis"), dict)
                else {}
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
                required=True,
            )
            terminal_persisted = True

            complete_trace_event = None
            try:
                complete_trace_event = trace_service.create_event(
                    trace_id=trace_id,
                    incident_id=incident_id,
                    node_name="workflow",
                    event_type="workflow_completed",
                    output_summary="AIOps workflow completed",
                    status=terminal_status,
                    metadata={"session_id": session_id},
                )
            except Exception as trace_exc:
                logger.warning(f"记录 AIOps 完成 Trace 失败: {trace_exc}")

            complete_progress_payload = progress_event_payload(complete_progress)
            complete_payload = {
                "type": "complete",
                "stage": "complete",
                "status": terminal_status,
                "message": "任务执行完成",
                "response": final_response,
                "incident_id": incident_id,
                "trace_id": trace_id,
                "pending_approval": pending_approval,
                "risk_assessment": risk_assessment,
                "structured_report": structured_report,
                "degradation_analysis": (
                    structured_report.get("degradation_analysis", {})
                    if isinstance(structured_report, dict)
                    else {}
                ),
            }
            if complete_trace_event is not None:
                complete_progress_payload = _attach_trace_event(
                    complete_progress_payload,
                    complete_trace_event,
                )
                complete_payload.update(
                    {
                        "trace_event_id": complete_trace_event.event_id,
                        "trace_event": complete_trace_event.model_dump(mode="json"),
                    }
                )
            yield complete_progress_payload
            yield attach_progress(complete_payload, complete_progress)

            logger.info(f"[会话 {session_id}] 任务执行完成")

        except AIOpsRunConflictError:
            raise
        except (asyncio.CancelledError, GeneratorExit):
            if terminal_persisted:
                raise
            interrupted_state = self._latest_runtime_state(locals())
            interrupted_progress = build_progress_payload(
                interrupted_state,
                phase="error",
                node_name="workflow",
                cursor=next_progress_cursor(),
                status="failed",
                report_status="failed",
                message="AIOps workflow interrupted before completion",
            )
            self._save_session_snapshot(
                session_id=session_id,
                state=state_with_progress(interrupted_state, interrupted_progress),
                status="failed",
                node_name="workflow",
            )
            try:
                trace_service.create_event(
                    trace_id=str(interrupted_state.get("trace_id") or "trace-unknown"),
                    incident_id=_extract_incident_id(interrupted_state),
                    node_name="workflow",
                    event_type="workflow_error",
                    output_summary="AIOps workflow interrupted before completion",
                    status="failed",
                    error_message="AIOps workflow interrupted before completion",
                    metadata={"session_id": session_id, "reason": "stream_interrupted"},
                )
            except Exception as exc:
                logger.warning(f"记录 AIOps 中断 Trace 失败: {exc}")
            raise
        except Exception as e:
            logger.error(
                f"[会话 {session_id}] 任务执行失败: "
                f"error_type={type(e).__name__}, {summarize_text_for_log(e, label='error')}"
            )
            public_message = public_exception_message(e, fallback=GENERIC_DIAGNOSIS_ERROR)
            error_state = self._latest_runtime_state(locals())
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
            error_trace_event = None
            try:
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
            except Exception as trace_exc:
                logger.warning(f"记录 AIOps 失败 Trace 失败: {trace_exc}")

            progress_payload = progress_event_payload(error_progress)
            error_payload: dict[str, Any] = {
                "type": "error",
                "stage": "error",
                "status": "failed",
                "message": public_message,
                "trace_id": str(error_state.get("trace_id") or "trace-unknown"),
            }
            if error_trace_event is not None:
                progress_payload = _attach_trace_event(progress_payload, error_trace_event)
                error_payload.update(
                    {
                        "trace_id": error_trace_event.trace_id,
                        "trace_event_id": error_trace_event.event_id,
                        "trace_event": error_trace_event.model_dump(mode="json"),
                    }
                )
            yield progress_payload
            yield attach_progress(error_payload, error_progress)
        finally:
            if run_claimed:
                self._release_diagnosis_session(session_id)

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
                    "degradation_analysis": (
                        (event.get("structured_report") or {}).get("degradation_analysis", {})
                        if isinstance(event.get("structured_report"), dict)
                        else {}
                    ),
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

    def reconcile_incomplete_runs(self) -> int:
        """Close runs abandoned by a process restart so they can be inspected or retried."""
        reconciled = 0
        offset = 0
        page_size = 100
        while True:
            snapshots = self.state_store.list_aiops_session_snapshots(
                limit=page_size,
                offset=offset,
            )
            if not snapshots:
                break
            offset += len(snapshots)
            for snapshot in snapshots:
                if snapshot.status not in {"running", "resume_running"}:
                    continue
                state = snapshot.to_state()
                state["errors"] = [
                    *list(state.get("errors") or []),
                    "AIOps workflow was abandoned by a process restart",
                ]
                if snapshot.status == "resume_running" and snapshot.resume_approval_id:
                    state["pending_approval"] = state.get("pending_approval") or {
                        "approval_id": snapshot.resume_approval_id,
                    }
                    state["resume_status"] = "failed"
                progress = build_progress_payload(
                    state,
                    phase="error",
                    node_name="workflow",
                    cursor=f"{snapshot.session_id}:restart-reconciled",
                    status="failed",
                    report_status="failed",
                    message="AIOps workflow was abandoned by a process restart",
                )
                if transition_session_snapshot(
                    self.state_store,
                    session_id=snapshot.session_id,
                    state=state_with_progress(state, progress),
                    status="failed",
                    node_name="workflow",
                    expected_statuses={snapshot.status},
                ):
                    reconciled += 1
            if len(snapshots) < page_size:
                break
        return reconciled

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
        return None

    def resolve_resume_session_id(
        self,
        *,
        incident_id: str,
        approval: ApprovalRequest,
        requested_session_id: str | None = None,
    ) -> str:
        """Resolve the paused run identity before opening the resume stream."""
        approval_session_id = str(approval.metadata.get("session_id") or "")
        if requested_session_id:
            if approval_session_id and approval_session_id != requested_session_id:
                raise ValueError("session_id does not belong to the approved diagnosis run")
        candidate_session_id = requested_session_id or approval_session_id
        if candidate_session_id:
            checkpoint = self.get_checkpoint_values(candidate_session_id)
            if checkpoint:
                if _extract_incident_id(checkpoint) != incident_id:
                    raise ValueError("session_id does not belong to the requested incident")
                self._validate_resume_state(checkpoint, approval)
                return candidate_session_id
            snapshot = self._load_resume_session_snapshot(
                session_id=candidate_session_id,
                incident_id=incident_id,
            )
            if snapshot is not None:
                self._validate_resume_state(snapshot.to_state(), approval)
                return candidate_session_id
            persisted_report = report_generator.get_report(incident_id)
            if persisted_report is not None:
                self._validate_resume_report(persisted_report, approval)
                return candidate_session_id
            raise LookupError(
                "No paused checkpoint, session snapshot, or persisted report "
                f"for incident {incident_id}"
            )

        offset = 0
        page_size = 100
        while True:
            snapshots = cast(
                list[AIOpsSessionSnapshot],
                self.state_store.list_aiops_session_snapshots(
                    incident_id=incident_id,
                    limit=page_size,
                    offset=offset,
                ),
            )
            if not snapshots:
                break
            offset += len(snapshots)
            for snapshot in snapshots:
                try:
                    self._validate_resume_state(snapshot.to_state(), approval)
                except ValueError:
                    continue
                return snapshot.session_id
            if len(snapshots) < page_size:
                break

        persisted_report = report_generator.get_report(incident_id)
        if persisted_report is not None:
            self._validate_resume_report(persisted_report, approval)
        else:
            raise LookupError(
                "No paused checkpoint, session snapshot, or persisted report "
                f"for incident {incident_id}"
            )
        return f"resume-{approval.approval_id}"

    async def resume_after_approval(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run one approval resume at most once concurrently."""
        self._claim_resume_approval(approval.approval_id)
        try:
            async for event in self._resume_after_approval_impl(
                session_id=session_id,
                incident_id=incident_id,
                approval=approval,
            ):
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            self._mark_resume_failed(
                session_id=session_id,
                incident_id=incident_id,
                approval=approval,
                message="AIOps approval resume interrupted before completion",
            )
            raise
        except AIOpsResumeConflictError:
            raise
        except Exception:
            self._mark_resume_failed(
                session_id=session_id,
                incident_id=incident_id,
                approval=approval,
                message="AIOps approval resume failed before completion",
            )
            raise
        finally:
            self._release_resume_approval(approval.approval_id)

    async def _resume_after_approval_impl(
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
        if ApprovalService.is_expired(approval):
            raise ValueError("approval authorization has expired")
        approval_session_id = str(approval.metadata.get("session_id") or "")
        if approval_session_id and approval_session_id != session_id:
            raise ValueError("session_id does not belong to the approved diagnosis run")

        values = self.get_checkpoint_values(session_id)
        resume_source = "checkpoint"
        persisted_report: DiagnosisReport | None = None
        if values:
            checkpoint_incident_id = _extract_incident_id(values)
            if checkpoint_incident_id != incident_id:
                raise ValueError("session_id does not belong to the requested incident")
            self._validate_resume_state(values, approval)
            trace_id = str(
                values.get("trace_id") or approval.metadata.get("trace_id") or "trace-unknown"
            )
        else:
            snapshot = self._load_resume_session_snapshot(
                session_id=session_id,
                incident_id=incident_id,
            )
            if snapshot is not None:
                values = snapshot.to_state()
                self._validate_resume_state(values, approval)
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
                self._validate_resume_report(persisted_report, approval)
                trace_id = persisted_report.trace_id or str(
                    approval.metadata.get("trace_id") or "trace-unknown"
                )
                resume_source = "report_fallback"

        progress_index = 0

        resume_state = dict(values or {})
        if not resume_state:
            resume_state = {
                "session_id": session_id,
                "trace_id": trace_id,
                "incident": {"incident_id": incident_id},
                "pending_approval": approval.model_dump(mode="json"),
            }
        existing_resume_snapshot = self.get_session_snapshot(session_id)
        resume_attempt = max(
            int((existing_resume_snapshot.resume_attempt if existing_resume_snapshot else 0) or 0)
            + 1,
            1,
        )

        def next_resume_progress_cursor() -> str:
            nonlocal progress_index
            progress_index += 1
            attempt_segment = "" if resume_attempt == 1 else f"{resume_attempt:02d}-"
            return f"{session_id}:resume-{attempt_segment}{progress_index:06d}"

        resume_state["resume_attempt"] = resume_attempt
        resume_progress = build_progress_payload(
            resume_state,
            phase="approval",
            node_name="workflow",
            cursor=next_resume_progress_cursor(),
            status="running",
            report_status="generating",
            message="Approved decision recorded; resuming diagnosis",
        )
        resume_snapshot_state = state_with_progress(resume_state, resume_progress)
        resume_snapshot_state["resume_approval_id"] = approval.approval_id
        resume_snapshot_state["resume_status"] = "running"
        resume_snapshot_state["resume_attempt"] = resume_attempt
        if not self._claim_resume_snapshot(
            session_id=session_id,
            state=resume_snapshot_state,
        ):
            raise AIOpsResumeConflictError(
                "diagnosis approval resume was already started or completed"
            )
        if resume_source == "report_fallback":
            persisted_report = self._load_validated_resume_report(
                incident_id=incident_id,
                approval=approval,
            )

        resume_event = None
        try:
            resume_event = trace_service.create_event(
                trace_id=trace_id,
                incident_id=incident_id,
                node_name="workflow",
                event_type="diagnosis_resumed",
                input_summary=f"approval_id={approval.approval_id}",
                output_summary=(
                    "Approved human decision recorded; agent will not execute production change"
                ),
                status="success",
                metadata={
                    "session_id": session_id,
                    "approval_id": approval.approval_id,
                    "approval_status": approval.status,
                    "resume_source": resume_source,
                    "boundary": "agent_does_not_execute_production_change",
                },
            )
        except Exception as exc:
            logger.warning(f"记录 AIOps resume Trace 失败: {exc}")

        resume_progress_payload = progress_event_payload(resume_progress)
        resume_payload: dict[str, Any] = {
            "type": "status",
            "stage": "diagnosis_resumed",
            "status": "running",
            "message": "审批已通过，正在记录人工决策并更新诊断报告；Agent 不会自动执行生产变更",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "resume_source": resume_source,
            "execution_boundary": "agent_does_not_execute_production_change",
        }
        if resume_event is not None:
            resume_progress_payload = _attach_trace_event(resume_progress_payload, resume_event)
            resume_payload.update(
                {
                    "trace_event_id": resume_event.event_id,
                    "trace_event": resume_event.model_dump(mode="json"),
                }
            )
        yield resume_progress_payload
        yield attach_progress(resume_payload, resume_progress)

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
            resumed_state["resume_approval_id"] = approval.approval_id
            resumed_state["resume_status"] = "completed"
            resumed_state["resume_attempt"] = resume_attempt
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
                status="resume_running",
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
                "resume_approval_id": approval.approval_id,
                "resume_status": "completed",
                "resume_attempt": resume_attempt,
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
                status="resume_running",
                node_name="workflow",
            )

        report_event = None
        try:
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
        except Exception as exc:
            logger.warning(f"记录 AIOps resumed report Trace 失败: {exc}")

        report_progress_payload = progress_event_payload(report_progress)
        report_payload = {
            "type": "report",
            "stage": "resumed_report",
            "status": report.status,
            "message": "审批结果已写入诊断报告，生产动作仍需通过安全变更流程处理",
            "incident_id": incident_id,
            "trace_id": trace_id,
            "resume_source": resume_source,
            "execution_boundary": "agent_does_not_execute_production_change",
            "report": report.markdown,
            "structured_report": report.model_dump(mode="json"),
        }
        if report_event is not None:
            report_progress_payload = _attach_trace_event(report_progress_payload, report_event)
            report_payload.update(
                {
                    "trace_event_id": report_event.event_id,
                    "trace_event": report_event.model_dump(mode="json"),
                }
            )
        yield report_progress_payload
        yield attach_progress(report_payload, report_progress)

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
            required=True,
        )
        complete_event = None
        try:
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
        except Exception as exc:
            logger.warning(f"记录 AIOps resume 完成 Trace 失败: {exc}")

        complete_progress_payload = progress_event_payload(complete_progress)
        complete_payload = {
            "type": "complete",
            "stage": "resume_complete",
            "status": report.status,
            "message": "审批结果记录完成，诊断闭环已更新；Agent 未执行任何生产变更",
            "incident_id": incident_id,
            "trace_id": trace_id,
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
        if complete_event is not None:
            complete_progress_payload = _attach_trace_event(
                complete_progress_payload,
                complete_event,
            )
            complete_payload.update(
                {
                    "trace_event_id": complete_event.event_id,
                    "trace_event": complete_event.model_dump(mode="json"),
                }
            )
        yield complete_progress_payload
        yield attach_progress(complete_payload, complete_progress)

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

    def _mark_resume_failed(
        self,
        *,
        session_id: str,
        incident_id: str,
        approval: ApprovalRequest,
        message: str,
    ) -> None:
        """Best-effort transition for a claimed resume that did not complete."""
        snapshot = self.get_session_snapshot(session_id)
        if snapshot is None or snapshot.status != "resume_running":
            return

        state = snapshot.to_state()
        state["resume_approval_id"] = approval.approval_id
        state["resume_status"] = "failed"
        state["pending_approval"] = approval.model_dump(mode="json")
        errors = list(state.get("errors") or [])
        errors.append(message)
        state["errors"] = errors
        progress = build_progress_payload(
            state,
            phase="error",
            node_name="workflow",
            cursor=f"{session_id}:resume-failed",
            status="failed",
            report_status="failed",
            message=message,
        )
        transitioned = transition_session_snapshot(
            self.state_store,
            session_id=session_id,
            state=state_with_progress(state, progress),
            status="failed",
            node_name="workflow",
            expected_statuses={"resume_running"},
        )
        if not transitioned:
            return
        try:
            trace_service.create_event(
                trace_id=snapshot.trace_id or str(approval.metadata.get("trace_id") or ""),
                incident_id=incident_id,
                node_name="workflow",
                event_type="resume_error",
                output_summary=message,
                status="failed",
                error_message=message,
                metadata={
                    "session_id": session_id,
                    "approval_id": approval.approval_id,
                },
            )
        except Exception as exc:
            logger.warning(f"记录 AIOps resume 失败 Trace 失败: {exc}")

    def _claim_resume_snapshot(
        self,
        *,
        session_id: str,
        state: dict[str, Any],
    ) -> bool:
        """Atomically claim a paused snapshot, including report-only recovery."""
        existing = self.get_session_snapshot(session_id)
        if existing is None:
            return create_session_snapshot(
                self.state_store,
                session_id=session_id,
                state=state,
                status="resume_running",
                node_name="workflow",
            )
        return transition_session_snapshot(
            self.state_store,
            session_id=session_id,
            state=state,
            status="resume_running",
            node_name="workflow",
            expected_statuses={"waiting_approval", "approval_approved", "failed"},
        )

    @staticmethod
    def _load_validated_resume_report(
        *,
        incident_id: str,
        approval: ApprovalRequest,
    ) -> DiagnosisReport:
        """Reload the report after claiming resume to avoid stale fallback state."""
        persisted_report = report_generator.get_report(incident_id)
        if persisted_report is None:
            raise LookupError(f"No persisted report for incident {incident_id}")
        AIOpsService._validate_resume_report(persisted_report, approval)
        return persisted_report

    def _save_session_snapshot(
        self,
        *,
        session_id: str,
        state: dict[str, Any],
        status: str,
        node_name: str,
        required: bool = False,
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
            if required:
                raise RuntimeError("AIOps session snapshot persistence failed") from exc

    @staticmethod
    def _latest_runtime_state(local_values: dict[str, Any]) -> dict[str, Any]:
        """Return the newest state available while unwinding execute()."""
        for key in (
            "final_snapshot_state",
            "final_values",
            "snapshot_state",
            "initial_snapshot_state",
            "initial_state",
        ):
            value = local_values.get(key)
            if isinstance(value, dict) and value:
                return dict(value)
        return {}

    @staticmethod
    def _validate_resume_state(
        state: dict[str, Any],
        approval: ApprovalRequest,
    ) -> None:
        """Ensure a resume targets the paused run that created the approval."""
        pending_approval = state.get("pending_approval")
        if not isinstance(pending_approval, dict):
            raise ValueError("diagnosis run is not waiting for the requested approval")
        pending_approval_id = str(pending_approval.get("approval_id") or "")
        if pending_approval_id != approval.approval_id:
            raise ValueError("approval_id does not belong to the requested diagnosis run")

    @staticmethod
    def _validate_resume_report(
        report: DiagnosisReport,
        approval: ApprovalRequest,
    ) -> None:
        """Ensure a report fallback belongs to the approval being resumed."""
        approval_trace_id = str(approval.metadata.get("trace_id") or "")
        if approval_trace_id and report.trace_id and approval_trace_id != report.trace_id:
            raise ValueError("approval_id does not belong to the persisted diagnosis report")
        approval_decision = report.approval_decision or {}
        report_approval_id = str(approval_decision.get("approval_id") or "")
        if report_approval_id and report_approval_id != approval.approval_id:
            raise ValueError("approval_id does not belong to the persisted diagnosis report")
        if report.status not in {"waiting_approval", "approval_approved"}:
            raise ValueError("persisted diagnosis report is not waiting for approval resume")

    def _claim_diagnosis_session(self, session_id: str) -> None:
        """Prevent workflows from concurrently sharing or reusing one run identity."""
        with self._active_run_lock:
            if session_id in self._active_diagnosis_sessions:
                raise AIOpsRunConflictError("session_id is already running")
            self._active_diagnosis_sessions.add(session_id)

    def _release_diagnosis_session(self, session_id: str) -> None:
        with self._active_run_lock:
            self._active_diagnosis_sessions.discard(session_id)

    def _claim_resume_approval(self, approval_id: str) -> None:
        """Prevent duplicate resume work for the same approved decision."""
        with self._active_run_lock:
            if approval_id in self._active_resume_approvals:
                raise AIOpsResumeConflictError("approval resume is already in progress")
            self._active_resume_approvals.add(approval_id)

    def _release_resume_approval(self, approval_id: str) -> None:
        with self._active_run_lock:
            self._active_resume_approvals.discard(approval_id)

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
