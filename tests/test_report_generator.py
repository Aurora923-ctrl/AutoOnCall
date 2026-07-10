"""Tests for deterministic AIOps diagnosis report generation."""

import importlib

import pytest
from fastapi import HTTPException

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.models.evidence import Evidence
from app.models.trace import ToolCallRecord
from app.services.change_plan_builder import build_change_plan
from app.services.report_generator import ReportGenerator
from app.services.trace_service import TraceService

replanner_module = importlib.import_module("app.agent.aiops.replanner")


def _state_with_redis_evidence() -> dict:
    state = create_initial_aiops_state(
        "order-service Redis connection timeout",
        session_id="report-generator-test",
    )
    state["incident"]["service_name"] = "order-service"
    state["incident"]["severity"] = "P1"
    state["incident"]["environment"] = "prod"
    state["hypotheses"] = ["Redis 连接数接近 maxclients，导致 order-service 连接超时"]
    state["gathered_evidence"] = [
        Evidence(
            source_tool="query_redis_status",
            step_id="s1",
            summary="connected_clients=9940/10000，Redis 连接数接近上限",
            evidence_type="redis",
            data_source="redis_info",
            stance="supporting",
            confidence_reason="Redis 连接数或慢日志阈值命中",
            raw_data={
                "status": "success",
                "output": {"summary": "connected_clients=9940/10000"},
            },
            confidence=0.82,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="query_metrics",
            step_id="s2",
            summary="order-service P95=3250ms，5xx=8.2%",
            evidence_type="metric",
            data_source="prometheus",
            stance="supporting",
            confidence_reason="Prometheus 显示用户侧错误率和延迟升高",
            raw_data={
                "status": "success",
                "output": {"summary": "P95=3250ms, 5xx=8.2%", "source": "prometheus"},
            },
            confidence=0.82,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="search_history_ticket",
            step_id="s3",
            summary="历史工单 INC-REDIS-001 命中 Redis maxclients 相似故障",
            evidence_type="ticket",
            data_source="ticket_api",
            stance="supporting",
            confidence_reason="历史工单可作为处置参考",
            raw_data={
                "status": "success",
                "output": {"summary": "similar Redis maxclients incident", "source": "ticket_api"},
            },
            confidence=0.66,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="search_runbook",
            step_id="s4",
            summary="Runbook confirms Redis maxclients timeout investigation steps",
            evidence_type="runbook",
            data_source="rag",
            stance="supporting",
            confidence_reason="Runbook retrieval matched Redis maxclients guidance.",
            raw_data={
                "status": "success",
                "output": {
                    "summary": "Redis maxclients runbook",
                    "retrieval_results": [
                        {
                            "source_file": "redis_postmortem.pdf",
                            "chunk_id": "redis-postmortem-001",
                        },
                        {
                            "source_file": "payment_wiki.html",
                            "chunk_id": "payment-wiki-001",
                        },
                    ],
                },
            },
            confidence=0.7,
        ).model_dump(mode="json"),
    ]
    state["tool_call_records"] = [
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s1",
            tool_name="query_redis_status",
            input_args={"service_name": "order-service"},
            output={"summary": "connected_clients=9940/10000"},
            data_source="redis_info",
            latency_ms=18.5,
            status="success",
        ).model_dump(mode="json"),
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s2",
            tool_name="query_metrics",
            input_args={"service_name": "order-service"},
            output={"summary": "P95=3250ms, 5xx=8.2%", "source": "prometheus"},
            data_source="prometheus",
            latency_ms=20.0,
            status="success",
        ).model_dump(mode="json"),
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s3",
            tool_name="search_history_ticket",
            input_args={"service_name": "order-service"},
            output={"summary": "similar Redis maxclients incident", "source": "ticket_api"},
            data_source="ticket_api",
            latency_ms=15.0,
            status="success",
        ).model_dump(mode="json"),
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s4",
            tool_name="search_runbook",
            input_args={"query": "Redis maxclients timeout"},
            output={
                "summary": "Redis maxclients runbook",
                "retrieval_results": [{"source_file": "redis_postmortem.pdf"}],
            },
            data_source="rag",
            latency_ms=16.0,
            status="success",
        ).model_dump(mode="json"),
    ]
    return state


def test_report_generator_builds_persists_and_reloads_report(tmp_path) -> None:
    trace_store = TraceService(tmp_path / "traces.db")
    state = _state_with_redis_evidence()
    state["warnings"] = ["步骤 s9 使用了 LLM ToolNode 兜底路径，结果需用标准工具复核。"]
    trace_event = trace_store.create_event(
        trace_id=state["trace_id"],
        incident_id=state["incident"]["incident_id"],
        node_name="executor",
        event_type="tool_call",
        output_summary="Redis 连接数接近上限",
    )

    generator = ReportGenerator(tmp_path / "reports.db")
    report = generator.generate_from_state(
        state,
        trace_events=[trace_event],
        status="completed",
    )

    assert report.incident_id == state["incident"]["incident_id"]
    assert report.trace_id == state["trace_id"]
    assert "Redis" in report.root_cause
    assert report.tool_calls[0]["tool_name"] == "query_redis_status"
    assert report.trace_summary["event_count"] == 1
    assert report.confirmed_facts
    assert report.inferred_conclusions
    assert report.next_steps
    assert "## 附录 A. 面试速览" in report.markdown
    assert "### Tool Call Table" in report.markdown
    assert "### Evidence Quick View" in report.markdown
    assert "| Tool | Source | Status | Latency ms | Artifact | Summary |" in report.markdown
    assert "| supporting | 4 |" in report.markdown
    assert "## 1. 故障摘要" in report.markdown
    assert "## 2. 影响范围" in report.markdown
    assert "## 3. 初步根因" in report.markdown
    assert "## 4. 关键证据" in report.markdown
    assert "## 5. 排查过程" in report.markdown
    assert "## 6. 风险动作判断" in report.markdown
    assert "## 7. 建议处置" in report.markdown
    assert "## 8. 回滚 / 观察指标" in report.markdown
    assert "## 9. 未确认事项" in report.markdown
    assert "Evidence back-links" in report.markdown
    assert "未记录到明确 evidence_id" not in report.markdown
    assert report.hypothesis_ranking[0]["supporting_evidence_ids"]
    assert "Risk boundary: policy=allow" in report.markdown
    assert "does not directly perform production write actions" in report.markdown
    assert "关键证据" in report.markdown
    assert "## 附录 B. 证据审计" in report.markdown
    assert "### 已确认事实" in report.markdown
    assert "### 推断结论" in report.markdown
    assert "### 根因假设矩阵" in report.markdown
    assert "### 下一步建议" in report.markdown
    assert "### 证据质量" in report.markdown
    assert "### 数据源边界" in report.markdown
    assert "### 诊断链路证据" in report.markdown
    assert "### 证据矩阵" in report.markdown
    assert "Root-cause evidence closure" in report.markdown
    assert "closure: satisfied (live + knowledge/history)" in report.markdown
    assert report.evidence_graph["root_cause_closure"]["status"] == "closed"
    assert report.evidence_graph["root_cause_closure"]["live_evidence_ids"]
    assert (
        report.evidence_graph["root_cause_closure"]["knowledge_evidence_ids"]
        or report.evidence_graph["root_cause_closure"]["history_evidence_ids"]
    )
    graph_nodes = report.evidence_graph["nodes"]
    graph_edges = report.evidence_graph["edges"]
    assert any(node["node_type"] == "incident" for node in graph_nodes)
    assert any(node["node_type"] == "hypothesis" and node["selected"] for node in graph_nodes)
    assert any(node["node_type"] == "evidence" and node["layer"] == "live" for node in graph_nodes)
    assert any(
        node["node_type"] == "evidence" and node["layer"] == "knowledge" for node in graph_nodes
    )
    assert any(
        node["node_type"] == "evidence" and node["layer"] == "history" for node in graph_nodes
    )
    assert any(edge["relation"] == "supported_by" for edge in graph_edges)
    assert any(edge["relation"] == "grounded_in" for edge in graph_edges)
    assert "### Incident Evidence Graph" in report.markdown
    assert "root-cause closure: closed" in report.markdown
    assert "#### Key Edges" in report.markdown
    assert "### Live Evidence" in report.markdown
    assert "### Knowledge Basis" in report.markdown
    assert "### Historical Experience" in report.markdown
    assert "Layer role: current adapter or incident-window facts" in report.markdown
    assert "Layer role: Runbook, postmortem, or wiki material" in report.markdown
    assert "Layer role: tickets, deploy history, or tables" in report.markdown
    assert "layer=live" in report.markdown
    assert "layer=knowledge" in report.markdown
    assert "layer=history" in report.markdown
    assert "rca_role=root-cause-support" in report.markdown
    assert "citations=redis_postmortem.pdf#redis-postmortem-001" in report.markdown
    assert "payment_wiki.html#payment-wiki-001" in report.markdown
    assert "数据源分布" in report.markdown
    assert "失败工具" in report.markdown
    assert "## 运行告警" in report.markdown
    assert report.warnings == state["warnings"]
    assert state["warnings"][0] in report.uncertainties
    assert "type=redis" in report.markdown
    assert "source=" in report.markdown
    assert "置信度原因" in report.markdown
    assert report.confidence > 0.7
    assert report.status == "completed"
    assert report.evidence_sufficiency["complete"] is True
    assert report.conclusion_alignment["status"] == "aligned"
    assert report.conclusion_alignment["missing_fields"] == []
    assert report.conclusion_alignment["fields"]["root_cause"]["evidence_ids"]
    assert report.conclusion_alignment["fields"]["root_cause"]["citations"]
    assert report.conclusion_alignment["fields"]["remediation_suggestion"]["aligned"] is True
    assert "### Conclusion Alignment" in report.markdown
    assert "root_cause: aligned=true" in report.markdown
    assert "remediation_suggestion: aligned=true" in report.markdown

    reloaded = ReportGenerator(tmp_path / "reports.db")
    assert reloaded.get_report(report.incident_id).report_id == report.report_id


def test_report_generator_downgrades_completed_when_evidence_is_insufficient(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"] = state["gathered_evidence"][:1]
    state["tool_call_records"] = state["tool_call_records"][:1]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status in {"incomplete", "needs_human", "degraded"}
    assert report.status == "incomplete"
    assert report.evidence_graph["root_cause_closure"]["status"] == "incomplete"
    assert "knowledge_or_history" in report.evidence_graph["root_cause_closure"]["missing_layers"]
    assert report.confidence <= 0.55
    assert report.evidence_sufficiency["complete"] is False
    missing_text = " ".join(report.evidence_sufficiency["missing_evidence"])
    assert "现象侧证据" in missing_text
    assert "处置参考" in missing_text
    assert "报告由 completed 降级为 incomplete" in report.markdown
    assert "当前置信度上限：0.55" in report.markdown


def test_report_generator_downgrades_unaligned_conclusions(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"] = []
    state["tool_call_records"] = []
    state["evidence_analysis"] = {
        "evidence_profile": {
            "sufficiency": {
                "complete": True,
                "status": "complete",
            }
        }
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status == "needs_human"
    assert report.conclusion_alignment["status"] == "needs_human_confirmation"
    assert set(report.conclusion_alignment["missing_fields"]) == {
        "root_cause",
        "key_findings",
        "remediation_suggestion",
    }
    assert report.root_cause.startswith("待人工确认：")
    assert all(item.startswith("待人工确认：") for item in report.key_findings)
    assert report.remediation_suggestion.startswith("待人工确认：")
    assert "root_cause: aligned=false" in report.markdown
    assert "报告由 completed 降级为 needs_human" in report.markdown


def test_report_generator_explains_redis_live_info_vs_incident_evidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"][0]["raw_data"]["output"].update(
        {
            "source": "redis_info",
            "summary": (
                "incident_evidence=redis-cluster-prod connected_clients=9940/10000 "
                "from replay window Redis key autooncall:incident:order-service:redis-maxclients; "
                "live_info=current_runtime connected_clients=1/10000"
            ),
            "incident_evidence": {
                "_key": "autooncall:incident:order-service:redis-maxclients",
                "connected_clients": "9940",
                "maxclients": "10000",
                "source": "live-redis-seed",
            },
            "live_info": {
                "connected_clients": 1,
                "maxclients": 10000,
                "scope": "current container runtime state",
            },
            "evidence_window_note": (
                "live_info is current container runtime state; incident_evidence is replay "
                "incident-window evidence stored in Redis keys."
            ),
            "evidence_timeline": [
                {
                    "stage": "incident_evidence",
                    "fact": "Redis evidence key reports connected_clients=9940/maxclients=10000.",
                    "inference": "Redis client capacity was exhausted.",
                    "uncertainty": "Evidence is replay-window data.",
                }
            ],
        }
    )
    state["tool_call_records"][0]["output"] = state["gathered_evidence"][0]["raw_data"]["output"]
    state["tool_call_records"][0]["output_summary"] = state["gathered_evidence"][0]["summary"]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert "## 数据源边界" in report.markdown
    assert "live_info 是当前容器运行态" in report.markdown
    assert "incident_evidence 是回放事故窗口证据" in report.markdown
    assert "connected_clients=1/maxclients=10000" in report.markdown
    assert "connected_clients=9940/maxclients=10000" in report.markdown
    assert "不声称当前 Redis 仍处于连接打满状态" in report.markdown
    assert "### Redis Evidence Timeline" in report.markdown
    assert "stage=incident_evidence" in report.markdown


def test_report_generator_explains_mysql_runtime_vs_incident_evidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["incident"]["service_name"] = "payment-service"
    state["incident"]["title"] = "payment-service MySQLSlowQueryLatency"
    state["hypotheses"] = ["MySQL 慢查询、连接池等待或锁等待放大接口延迟。"]
    state["gathered_evidence"][0].update(
        {
            "source_tool": "query_mysql_status",
            "summary": "MySQL incident evidence shows slow_query_count=18, active_connections=188/200, pool_waiting=6.",
            "evidence_type": "mysql",
            "data_source": "mysql",
            "fact": "slow_query_count=18, active_connections=188/200, pool_waiting=6",
            "inference": "Slow SQL occupied connections and caused pool waiting.",
            "uncertainty": "Current Slow_queries runtime counter is 0; incident evidence carries the outage window.",
            "raw_data": {
                "status": "success",
                "output": {
                    "source": "mysql",
                    "incident_evidence": {
                        "observed_value": "slow_queries=18,pool_waiting=6,active_connections=188/200",
                    },
                    "live_status": {
                        "Slow_queries": 0,
                        "Threads_connected": 2,
                    },
                    "summary": "slow_queries=18, pool_waiting=6",
                },
            },
        }
    )
    state["tool_call_records"][0].update(
        {
            "tool_name": "query_mysql_status",
            "data_source": "mysql",
            "output": state["gathered_evidence"][0]["raw_data"]["output"],
        }
    )

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert "## 数据源边界" in report.markdown
    assert "MySQL：live_status 是当前容器运行态" in report.markdown
    assert "incident_evidence 是事故窗口证据" in report.markdown
    assert "Slow_queries=0" in report.markdown
    assert "pool_waiting=6" in report.markdown
    assert "不声称当前 Slow_queries runtime counter 仍在增长" in report.markdown


def test_report_generator_records_report_generated_trace(monkeypatch, tmp_path) -> None:
    report_generator_module = importlib.import_module("app.services.report_generator")
    trace_store = TraceService(tmp_path / "trace.db")
    state = _state_with_redis_evidence()
    trace_store.create_event(
        trace_id=state["trace_id"],
        incident_id=state["incident"]["incident_id"],
        node_name="workflow",
        event_type="workflow_started",
        output_summary="AIOps workflow started",
    )
    monkeypatch.setattr(report_generator_module, "trace_service", trace_store)

    report = report_generator_module.ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        status="completed",
    )

    report_events = trace_store.list_events(
        incident_id=report.incident_id,
        event_type="report_generated",
    )
    assert len(report_events) == 1
    assert report_events[0].trace_id == report.trace_id
    assert report_events[0].metadata["report_id"] == report.report_id


def test_report_generator_renders_runbook_references(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"].append(
        Evidence(
            source_tool="retrieve_knowledge",
            step_id="planner-runbook",
            summary="检索到 1 条可信知识来源",
            evidence_type="runbook",
            stance="supporting",
            confidence_reason="Runbook 检索命中",
            raw_data={
                "output": {
                    "status": "success",
                    "retrieval_results": [
                        {
                            "source_file": "cpu_high_usage.md",
                            "chunk_id": "cpu_high_usage.md#0001",
                            "heading_path": "CPU使用率过高告警处理方案",
                            "score": 0.2,
                            "content_preview": "CPU 使用率过高处理方案",
                        }
                    ],
                }
            },
            confidence=0.65,
        ).model_dump(mode="json")
    )

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert "## 附录 C. 工具、Trace 与 Runbook" in report.markdown
    assert "### Runbook 引用" in report.markdown
    assert "cpu_high_usage.md" in report.markdown
    assert "chunk=cpu_high_usage.md#0001" in report.markdown
    assert "score=0.2000" in report.markdown


def test_report_generator_omits_advanced_dependency_signals(tmp_path) -> None:
    generator = ReportGenerator(storage_path=tmp_path / "reports.db")
    report = generator.generate_from_state(
        {
            "incident": {"incident_id": "INC-001", "service_name": "order-service"},
            "trace_id": "trace-1",
            "gathered_evidence": [],
            "tool_call_records": [],
        }
    )

    assert report.dependency_signals == []


def test_report_generator_marks_pending_approval_as_manual_action(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["risk_assessment"] = {
        "risk_level": "high",
        "policy": "approval_required",
        "need_approval": True,
        "action": "调整 Redis maxclients 配置",
    }
    state["pending_approval"] = {
        "approval_id": "apr-1",
        "status": "pending",
        "action": "调整 Redis maxclients 配置",
        "risk_level": "high",
        "change_plan": {
            "change_plan_id": "chg-1",
            "incident_id": state["incident"]["incident_id"],
            "action": "调整 Redis maxclients 配置",
            "risk_level": "high",
            "status": "draft",
            "pre_checklist": ["确认 Redis 指标"],
            "execution_steps": ["人工调整配置"],
            "rollback_steps": ["恢复原配置"],
            "verification_steps": ["确认 5xx 恢复"],
            "manual_execution_required": True,
        },
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="waiting_approval",
    )

    assert report.manual_action_required is True
    assert report.approval_status == "pending"
    assert report.approval_decision["approval_id"] == "apr-1"
    assert report.approval_decision["action"] == "调整 Redis maxclients 配置"
    assert report.change_plan["change_plan_id"] == "chg-1"
    assert "Risk boundary: policy=approval_required" in report.markdown
    assert "manual_action_required=true" in report.markdown
    assert "等待人工审批" in report.markdown
    assert "审批动作：调整 Redis maxclients 配置" in report.markdown
    assert "### 人工动作与回滚边界" in report.markdown
    assert "### 变更计划草案" in report.markdown
    assert "人工调整配置" in report.markdown
    assert "Agent 只输出诊断和处置建议" in report.markdown
    assert "回滚方案" in report.markdown


def test_report_generator_renders_structured_remediation_playbook_from_plan_builder(
    tmp_path,
) -> None:
    state = _state_with_redis_evidence()
    plan = build_change_plan(
        incident_id=state["incident"]["incident_id"],
        action="调整 Redis maxclients 配置",
        risk_level="high",
        tool_name="manual_change_record",
        service_name="order-service",
        environment="prod",
        reason="Redis maxclients incident-window evidence is saturated.",
        metadata={"component": "redis", "scenario": "redis-maxclients"},
    )
    state["risk_assessment"] = {
        "risk_level": "high",
        "policy": "approval_required",
        "need_approval": True,
        "action": plan.action,
    }
    state["pending_approval"] = {
        "approval_id": "apr-playbook-1",
        "status": "pending",
        "action": plan.action,
        "risk_level": "high",
        "change_plan": plan.model_dump(mode="json"),
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="waiting_approval",
    )

    playbook = report.change_plan["remediation_playbook"]
    assert report.manual_action_required is True
    assert report.approval_status == "pending"
    assert playbook["approval_required"] is True
    assert playbook["risk_policy"] == "approval_required"
    assert playbook["dry_run"]
    assert playbook["rollback"]
    assert playbook["stop_conditions"]
    assert "结构化安全处置 Playbook" in report.markdown
    assert "Dry-run" in report.markdown
    assert "Rollback" in report.markdown
    assert "Stop conditions" in report.markdown
    assert "Safety notes" in report.markdown


def test_report_generator_marks_approval_decision_on_latest_report(tmp_path) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    state = _state_with_redis_evidence()
    state["risk_assessment"] = {
        "risk_level": "high",
        "policy": "approval_required",
        "need_approval": True,
        "action": "调整 Redis maxclients 配置",
    }
    state["pending_approval"] = {
        "approval_id": "apr-1",
        "status": "pending",
        "action": "调整 Redis maxclients 配置",
        "risk_level": "high",
        "change_plan": {
            "change_plan_id": "chg-1",
            "incident_id": state["incident"]["incident_id"],
            "action": "调整 Redis maxclients 配置",
            "risk_level": "high",
            "status": "draft",
            "pre_checklist": ["确认 Redis 指标"],
            "execution_steps": ["人工调整配置"],
            "rollback_steps": ["恢复原配置"],
            "verification_steps": ["确认 5xx 恢复"],
            "manual_execution_required": True,
        },
    }
    pending = generator.generate_from_state(state, trace_events=[], status="waiting_approval")

    updated = generator.mark_approval_decided(
        incident_id=pending.incident_id,
        approval_status="approved",
        decided_by="pytest",
        reason="approved for manual mitigation",
    )

    assert updated is not None
    assert updated.status == "approval_approved"
    assert updated.approval_status == "approved"
    assert updated.approval_decision["action"] == "调整 Redis maxclients 配置"
    assert updated.approval_decision["decided_by"] == "pytest"
    assert updated.approval_decision["decision_reason"] == "approved for manual mitigation"
    assert updated.approval_decision["decided_at"]
    assert updated.risk_summary["approval_decision"]["decided_by"] == "pytest"
    assert updated.change_plan["status"] == "approved"
    assert "审批状态：approved" in updated.markdown
    assert "审批动作：调整 Redis maxclients 配置" in updated.markdown
    assert "审批人：pytest" in updated.markdown
    assert "审批原因：approved for manual mitigation" in updated.markdown
    assert "Agent 不直接执行生产写操作" in updated.markdown
    assert "安全变更流程" in updated.markdown


def test_report_generator_renders_conflicts_and_confidence_reasons(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["evidence_analysis"] = {
        "decision": "generate_report",
        "reason": "检测到证据冲突，生成待确认报告",
        "conflicts": ["日志指向 Redis timeout，但 Redis connected_clients/maxclients 状态正常"],
        "missing_evidence": ["query_logs"],
        "evidence_profile": {
            "by_type": {"redis": 1},
            "by_stance": {"supporting": 1},
        },
        "confidence_reasons": [
            "query_redis_status: Redis 连接数或慢日志阈值命中",
            "证据冲突降低置信度: 日志指向 Redis timeout，但 Redis connected_clients/maxclients 状态正常",
        ],
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-redis",
                "title": "Redis 状态冲突，需要复核客户端连接池",
                "description": "Redis timeout 与 Redis 实例状态冲突",
                "category": "redis_maxclients",
                "supporting_evidence_ids": ["evd-log"],
                "refuting_evidence_ids": ["evd-redis"],
                "missing_evidence": ["query_network_status"],
                "confidence": 0.48,
                "confidence_reason": "存在支持和反驳证据",
            }
        ],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.uncertainties
    assert report.confidence_reason.startswith("query_redis_status")
    assert report.selected_root_cause_id == "hyp-redis"
    assert report.hypothesis_ranking[0]["refuting_evidence_ids"] == ["evd-redis"]
    assert "## 不确定性" in report.markdown
    assert "根因假设矩阵" in report.markdown
    assert "query_network_status" in report.markdown
    assert "证据冲突降低置信度" in report.markdown


def test_report_generator_uses_ranked_root_cause_confidence_signal(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"][0]["confidence"] = 0.4
    state["evidence_analysis"] = {
        "confidence": 0.72,
        "confidence_reasons": ["已收集至少三类成功证据，并形成可解释根因假设"],
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-redis",
                "title": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "description": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "category": "redis_maxclients",
                "supporting_evidence_ids": ["evd-redis", "evd-metrics", "evd-logs"],
                "refuting_evidence_ids": [],
                "missing_evidence": [],
                "confidence": 0.76,
                "confidence_reason": "症状或证据文本命中该场景关键词；3 条支持证据",
            }
        ],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence == 0.76


def test_report_generator_keeps_graceful_degradation_confidence_floor(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"] = [
        Evidence(
            source_tool="query_k8s_status",
            step_id="s1",
            summary="工具 query_k8s_status 调用失败: Kubernetes API RBAC 权限不足",
            evidence_type="k8s",
            data_source="unknown",
            stance="neutral",
            confidence_reason="工具失败: Kubernetes API RBAC 权限不足",
            raw_data={"status": "failed", "error_message": "Kubernetes API RBAC 权限不足"},
            confidence=0.1,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="query_logs",
            step_id="s2",
            summary="日志发现 timeout 异常",
            evidence_type="log",
            data_source="mock",
            stance="supporting",
            confidence_reason="日志关键词命中",
            raw_data={"status": "success", "output": {"summary": "日志发现 timeout 异常"}},
            confidence=0.75,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="query_metrics",
            step_id="s3",
            summary="P95 和 5xx 异常升高",
            evidence_type="metric",
            data_source="mock",
            stance="supporting",
            confidence_reason="指标阈值命中",
            raw_data={"status": "success", "output": {"summary": "P95 和 5xx 异常升高"}},
            confidence=0.75,
        ).model_dump(mode="json"),
        Evidence(
            source_tool="search_runbook",
            step_id="s4",
            summary="Runbook 命中 Pod 排障手册",
            evidence_type="runbook",
            data_source="rag",
            stance="supporting",
            confidence_reason="Runbook 检索命中",
            raw_data={"status": "success", "output": {"summary": "Runbook 命中"}},
            confidence=0.72,
        ).model_dump(mode="json"),
    ]
    state["evidence_analysis"] = {
        "confidence": 0.53,
        "confidence_reasons": ["query_k8s_status: 工具失败", "query_logs: 日志关键词命中"],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence == 0.5
    assert "### Other / Uncertain Evidence" in report.markdown
    assert "query_k8s_status" in report.markdown


def test_report_generator_caps_mock_only_analysis_confidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    for item in state["gathered_evidence"]:
        if item.get("evidence_type") not in {"runbook", "risk"}:
            item["data_source"] = "mock"
            item["raw_data"] = {
                "status": "success",
                "output": {"source": "mock", "summary": item.get("summary", "")},
            }
            item["confidence"] = 0.5
    state["evidence_analysis"] = {
        "confidence": 0.9,
        "evidence_profile": {
            "source_quality": "fallback_only",
            "diagnostic_success_count": 3,
            "trusted_source_count": 0,
            "fallback_source_count": 3,
        },
        "hypothesis_ranking": [
            {
                "title": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "confidence": 0.92,
                "confidence_reason": "mock 证据命中，但缺少真实数据源",
            }
        ],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence == 0.5


def test_report_generator_caps_unknown_successful_evidence_confidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    for item in state["gathered_evidence"]:
        if item.get("evidence_type") not in {"runbook", "risk"}:
            item["data_source"] = "unknown"
            item["raw_data"] = {
                "status": "success",
                "output": {"summary": item.get("summary", "")},
            }
            item["confidence"] = 0.9
    state["evidence_analysis"] = {
        "confidence": 0.88,
        "hypothesis_ranking": [
            {
                "title": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "confidence": 0.9,
                "confidence_reason": "未知来源证据命中，需要复核",
            }
        ],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence == 0.5


@pytest.mark.asyncio
async def test_replanner_response_generation_attaches_structured_report(
    monkeypatch, tmp_path
) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    monkeypatch.setattr(replanner_module, "report_generator", generator)
    monkeypatch.setattr(replanner_module, "_create_llm", lambda: object())

    async def fake_generate_response(state, llm):
        return {"response": "# legacy markdown"}

    monkeypatch.setattr(replanner_module, "_generate_response", fake_generate_response)

    state = _state_with_redis_evidence()
    analysis = EvidenceAnalysis(
        decision="generate_report",
        reason="证据覆盖 Redis 状态和错误症状",
        evidence_sufficient=True,
        hypotheses=state["hypotheses"],
        confidence=0.82,
    )

    update = await replanner_module._generate_response_with_analysis(state, analysis)

    assert update["report"]["incident_id"] == state["incident"]["incident_id"]
    assert update["response"] == update["report"]["markdown"]
    assert "# legacy markdown" not in update["response"]
    assert "Redis" in update["final_diagnosis"]
    assert generator.get_report(state["incident"]["incident_id"]) is not None


@pytest.mark.asyncio
async def test_replanner_report_generation_survives_llm_creation_failure(
    monkeypatch,
    tmp_path,
) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    monkeypatch.setattr(replanner_module, "report_generator", generator)
    monkeypatch.setattr(
        replanner_module,
        "_create_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("missing api key")),
    )

    state = _state_with_redis_evidence()
    analysis = EvidenceAnalysis(
        decision="generate_report",
        reason="证据覆盖 Redis 状态和错误症状",
        evidence_sufficient=True,
        hypotheses=state["hypotheses"],
        confidence=0.82,
    )

    update = await replanner_module._generate_response_with_analysis(state, analysis)

    assert update["report"]["incident_id"] == state["incident"]["incident_id"]
    assert "Redis" in update["response"]


@pytest.mark.asyncio
async def test_incident_report_api_returns_latest_report(monkeypatch, tmp_path) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    state = _state_with_redis_evidence()
    report = generator.generate_from_state(state, trace_events=[])

    incidents_api = importlib.import_module("app.api.incidents")
    monkeypatch.setattr(incidents_api, "get_report_generator", lambda: generator)

    result = await incidents_api.get_incident_report(report.incident_id)
    markdown_result = await incidents_api.get_incident_report(
        report.incident_id,
        response_format="markdown",
    )

    assert result["report"]["report_id"] == report.report_id
    assert result["markdown"] == report.markdown
    assert markdown_result["markdown"] == report.markdown

    with pytest.raises(HTTPException):
        await incidents_api.get_incident_report("inc-missing")
