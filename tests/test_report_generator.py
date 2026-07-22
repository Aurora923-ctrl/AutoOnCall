"""Tests for deterministic AIOps diagnosis report generation."""

import importlib

import pytest
from fastapi import HTTPException

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.models.evidence import Evidence
from app.models.trace import ToolCallRecord, TraceEvent
from app.services.change_plan_builder import build_change_plan
from app.services.evidence_graph import build_incident_evidence_graph
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
    redis_evidence_id = state["gathered_evidence"][0]["evidence_id"]
    reference_evidence_ids = [
        item["evidence_id"]
        for item in state["gathered_evidence"]
        if item["evidence_type"] in {"ticket", "runbook"}
    ]
    state["evidence_analysis"] = {
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-redis-maxclients",
                "title": state["hypotheses"][0],
                "description": state["hypotheses"][0],
                "category": "redis_maxclients",
                "supporting_evidence_ids": [redis_evidence_id, *reference_evidence_ids],
                "refuting_evidence_ids": [],
                "missing_evidence": [],
                "confidence": 0.82,
                "confidence_reason": "Redis live status directly supports maxclients saturation.",
            }
        ]
    }
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
    assert "## 附录 A. Evidence、Graph 与 Citation" in report.markdown
    assert "## 附录 B. ToolCall 与 Trace" in report.markdown
    assert "### Citation / Runbook 引用" in report.markdown
    assert (
        "query_redis_status step=s1 source=redis_info status=success latency_ms=18.5"
        in report.markdown
    )
    assert report.evidence_profile["by_stance"]["supporting"] == 4
    assert "## 1. 故障摘要" in report.markdown
    assert "## 2. 用户影响" in report.markdown
    assert "## 3. 初步根因" in report.markdown
    assert "## 4. 关键证据" in report.markdown
    assert "## 5. 排查过程" in report.markdown
    assert "## 6. 风险动作判断" in report.markdown
    assert "## 7. 建议处置" in report.markdown
    assert "## 8. 回滚与观察指标" in report.markdown
    assert "## 9. 未确认事项" in report.markdown
    assert "Evidence back-links" in report.markdown
    assert "未记录到明确 evidence_id" not in report.markdown
    assert report.hypothesis_ranking[0]["supporting_evidence_ids"]
    assert "关键证据" in report.markdown
    assert "### 已确认事实" in report.markdown
    assert "### 推断结论" in report.markdown
    assert "### 根因假设矩阵" in report.markdown
    assert "### 证据质量" in report.markdown
    assert "### 数据源边界" in report.markdown
    assert "### 诊断链路证据" in report.markdown
    assert "### 证据矩阵" in report.markdown
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
    first_screen = report.markdown.split("## 2. 用户影响", 1)[0]
    assert "order-service" in first_screen
    assert "P1" in first_screen
    assert "初步根因" in first_screen
    assert "置信度" in first_screen
    key_evidence_body = report.markdown.split("## 4. 关键证据", 1)[1].split("## 5. 排查过程", 1)[0]
    assert key_evidence_body.count("\n| evd-") <= 5
    assert "| Evidence | Tool / Source | Fact | Inference | Uncertainty |" in report.markdown
    investigation = report.markdown.split("## 5. 排查过程", 1)[1].split("## 6. 风险动作判断", 1)[0]
    assert "query_redis_status" in investigation
    assert "18.5 ms" in investigation

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


def test_report_body_shows_failed_tool_without_dumping_raw_json(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["tool_call_records"].append(
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s5",
            tool_name="query_logs",
            input_args={"service_name": "order-service", "token": "secret-value"},
            output={"status": "failed", "raw": {"very_large": ["x"] * 20}},
            output_summary="",
            data_source="failed",
            latency_ms=120.0,
            status="failed",
            error_message="Loki timeout",
        ).model_dump(mode="json")
    )
    state["errors"] = ["工具 query_logs 调用失败: Loki timeout"]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    body = report.markdown.split("## 附录 A.", 1)[0]
    investigation = body.split("## 5. 排查过程", 1)[1].split("## 6. 风险动作判断", 1)[0]
    assert "query_logs" in investigation
    assert "failed" in investigation
    assert "Loki timeout" in investigation
    assert "secret-value" not in body
    assert '"very_large"' not in body


def test_report_classifies_timeout_degradation_as_unsafe_and_needing_human(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["tool_call_records"].append(
        ToolCallRecord(
            trace_id=state["trace_id"],
            incident_id=state["incident"]["incident_id"],
            step_id="s5",
            tool_name="query_logs",
            input_args={"service_name": "order-service"},
            output={"status": "failed"},
            output_summary="",
            data_source="failed",
            latency_ms=120000.0,
            status="failed",
            error_message="Loki request timeout",
        ).model_dump(mode="json")
    )
    state["errors"] = ["工具 query_logs 调用失败: Loki request timeout"]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status == "degraded"
    assert report.manual_action_required is True
    assert report.degradation_analysis["category"] == "dependency_timeout"
    assert report.degradation_analysis["safe_terminal"] is False
    assert report.degradation_analysis["needs_human"] is True
    assert report.degradation_analysis["failed_tools"] == ["query_logs"]
    assert "降级根因：dependency_timeout" in report.markdown


def test_report_classifies_evidence_only_degradation_as_safe_boundary(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"] = [
        item
        for item in state["gathered_evidence"]
        if item["source_tool"] not in {"search_runbook", "search_history_ticket"}
    ]
    state["tool_call_records"] = [
        item
        for item in state["tool_call_records"]
        if item["tool_name"] not in {"search_runbook", "search_history_ticket"}
    ]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status == "needs_human"
    assert report.manual_action_required is True
    assert report.degradation_analysis["category"] == "evidence_insufficient"
    assert report.degradation_analysis["safe_terminal"] is True
    assert report.degradation_analysis["missing_evidence"]


def test_report_recommendation_contains_complete_change_loop(tmp_path) -> None:
    state = _state_with_redis_evidence()
    plan = build_change_plan(
        incident_id=state["incident"]["incident_id"],
        action="调整 Redis maxclients 配置",
        risk_level="high",
        tool_name="apply_config_change",
        service_name="order-service",
        environment="prod",
        reason="Redis incident-window evidence is saturated.",
    )
    state["risk_assessment"] = {
        "risk_level": "high",
        "policy": "approval_required",
        "need_approval": True,
        "action": plan.action,
    }
    state["pending_approval"] = {
        "approval_id": "apr-report-loop",
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

    recommendation = report.markdown.split("## 7. 建议处置", 1)[1].split("## 8. 回滚与观察指标", 1)[
        0
    ]
    assert "前置检查" in recommendation
    assert "审批边界" in recommendation
    assert "Dry-run" in recommendation
    assert "观察" in recommendation
    assert "回滚" in recommendation


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
        "key_findings",
        "remediation_suggestion",
    }
    assert report.conclusion_alignment["fields"]["root_cause"]["claim_type"] == "insufficiency"
    assert report.root_cause == "证据不足，暂未形成明确根因"
    assert all(item.startswith("待人工确认：") for item in report.key_findings)
    assert report.remediation_suggestion.startswith("待人工确认：")
    assert "root_cause: aligned=true" in report.markdown
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


def test_report_generator_uses_the_injected_trace_repository(monkeypatch, tmp_path) -> None:
    report_generator_module = importlib.import_module("app.services.report_generator")
    state = _state_with_redis_evidence()
    trace_store = TraceService(tmp_path / "trace.db")
    trace_store.create_event(
        trace_id=state["trace_id"],
        incident_id=state["incident"]["incident_id"],
        node_name="workflow",
        event_type="workflow_started",
    )

    monkeypatch.setattr(report_generator_module, "trace_service", trace_store)
    generator = report_generator_module.ReportGenerator(tmp_path / "reports.db")
    report = generator.generate_from_state(state, status="completed")

    assert [
        event.event_type
        for event in trace_store.list_events(
            incident_id=report.incident_id,
            event_type="report_generated",
        )
    ] == ["report_generated"]


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

    assert "## 附录 B. ToolCall 与 Trace" in report.markdown
    assert "### Citation / Runbook 引用" in report.markdown
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
    assert "风险策略：approval_required" in report.markdown
    assert "是否需要人工动作：是" in report.markdown
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
    assert report.selected_root_cause_id == ""
    assert "证据不足" in report.root_cause
    assert report.hypothesis_ranking[0]["refuting_evidence_ids"] == []
    assert "## 不确定性" in report.markdown
    assert "根因假设矩阵" in report.markdown
    assert "query_network_status" in report.markdown
    assert "证据冲突降低置信度" in report.markdown


def test_report_generator_uses_ranked_root_cause_confidence_signal(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"][0]["confidence"] = 0.4
    supporting_ids = [item["evidence_id"] for item in state["gathered_evidence"][:3]]
    state["evidence_analysis"] = {
        "confidence": 0.72,
        "confidence_reasons": ["已收集至少三类成功证据，并形成可解释根因假设"],
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-redis",
                "title": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "description": "Redis maxclients 或连接池耗尽导致 timeout 和 5xx。",
                "category": "redis_maxclients",
                "supporting_evidence_ids": supporting_ids,
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


def test_report_generator_ignores_unsupported_hypothesis_confidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["evidence_analysis"] = {
        "confidence": 0.45,
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-unsupported",
                "title": "Unsupported root cause",
                "supporting_evidence_ids": [],
                "confidence": 0.99,
            }
        ],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence < 0.99
    assert report.selected_root_cause_id == ""


def test_report_generator_does_not_use_unlinked_analysis_confidence(tmp_path) -> None:
    state = _state_with_redis_evidence()
    for item in state["gathered_evidence"]:
        if item.get("evidence_type") not in {"runbook", "risk"}:
            item["confidence"] = 0.2
    state["evidence_analysis"] = {
        "confidence": 0.99,
        "hypothesis_ranking": [],
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.confidence < 0.99


def test_report_generator_filters_supplied_trace_events_to_current_run(tmp_path) -> None:
    state = _state_with_redis_evidence()
    incident_id = state["incident"]["incident_id"]
    trace_id = state["trace_id"]
    events = [
        TraceEvent(
            trace_id="trace-other",
            incident_id=incident_id,
            node_name="executor",
            event_type="tool_call",
            output_summary="other run",
        ),
        TraceEvent(
            trace_id=trace_id,
            incident_id=incident_id,
            node_name="executor",
            event_type="tool_call",
            output_summary="current run",
        ),
    ]

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=events,
        status="completed",
    )

    assert report.trace_summary["event_count"] == 1
    assert len(report.timeline) == 1
    assert report.timeline[0]["summary"] == "current run"


def test_report_generator_does_not_present_unsupported_hypothesis_as_summary_or_finding(
    tmp_path,
) -> None:
    state = _state_with_redis_evidence()
    state["hypotheses"] = ["Database corruption caused the incident"]
    state["evidence_analysis"] = {}

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert "Database corruption" not in report.summary
    assert all("Database corruption" not in item for item in report.key_findings)
    assert all("Database corruption" not in item for item in report.inferred_conclusions)


def test_report_generator_does_not_treat_rule_based_suggestion_as_key_finding(tmp_path) -> None:
    state = _state_with_redis_evidence()
    suggestion = "Remediation suggestions generated; real changes require approval"
    state["gathered_evidence"].append(
        Evidence(
            source_tool="suggest_remediation",
            step_id="s5",
            summary=suggestion,
            fact=suggestion,
            evidence_type="risk",
            data_source="rule_based",
            stance="supporting",
            confidence_reason="Deterministic suggestion output, not an observed incident fact",
            raw_data={"status": "success", "output": {"summary": suggestion}},
            confidence=0.99,
        ).model_dump(mode="json")
    )

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status == "completed"
    assert all(suggestion not in finding for finding in report.key_findings)
    assert report.conclusion_alignment["status"] == "aligned"


def test_report_generator_redacts_secrets_before_persistence_and_markdown(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["incident"]["symptom"] = "authorization=Bearer incident-secret"
    state["gathered_evidence"][0]["fact"] = "token=evidence-secret"
    state["gathered_evidence"][0]["raw_data"]["output"]["password"] = "redis-secret"
    state["tool_call_records"][0]["input_args"] = {"api_key": "tool-secret"}
    state["tool_call_records"][0]["output_summary"] = "cookie=session-secret"

    generator = ReportGenerator(tmp_path / "reports.db")
    report = generator.generate_from_state(state, trace_events=[], status="completed")
    payload = report.model_dump_json()

    assert "incident-secret" not in payload
    assert "evidence-secret" not in payload
    assert "redis-secret" not in payload
    assert "tool-secret" not in payload
    assert "session-secret" not in payload
    assert "[REDACTED]" in report.markdown
    assert generator.get_report(report.incident_id).markdown == report.markdown


def test_report_generator_neutralizes_active_markdown_content(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["incident"]["title"] = "<script>alert(1)</script>"
    state["gathered_evidence"][0]["fact"] = "[click](javascript:alert(1))"

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert "<script>" not in report.markdown
    assert "&lt;script&gt;" in report.markdown
    assert "javascript:" not in report.markdown.lower()
    assert "javascript&#58;" in report.markdown.lower()


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


def test_completed_request_with_pending_approval_is_downgraded(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["pending_approval"] = {
        "approval_id": "approval-1",
        "status": "pending",
        "action": "change",
    }

    report = ReportGenerator(tmp_path / "approval.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.status == "waiting_approval"
    assert report.manual_action_required is True


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


def test_report_generator_does_not_select_unsupported_top_hypothesis(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"][0]["evidence_id"] = "evd-redis"
    state["evidence_analysis"] = {
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-unsupported",
                "title": "Unsupported root cause",
                "category": "dependency_timeout",
                "supporting_evidence_ids": [],
                "refuting_evidence_ids": [],
                "confidence": 0.9,
            },
            {
                "hypothesis_id": "hyp-supported",
                "title": "Redis maxclients saturation",
                "category": "redis_maxclients",
                "supporting_evidence_ids": ["evd-redis"],
                "refuting_evidence_ids": [],
                "confidence": 0.7,
            },
        ]
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.selected_root_cause_id == "hyp-supported"
    assert report.root_cause == "Redis maxclients saturation"


def test_report_generator_legacy_hypothesis_does_not_inherit_unrelated_evidence(
    tmp_path,
) -> None:
    state = _state_with_redis_evidence()
    state["hypotheses"] = ["Legacy unsupported root cause"]
    state["evidence_analysis"] = {}

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.selected_root_cause_id == ""
    assert report.hypothesis_ranking[0]["supporting_evidence_ids"] == []


def test_report_generator_without_ranking_does_not_promote_supporting_fact_to_root_cause(
    tmp_path,
) -> None:
    state = _state_with_redis_evidence()
    state["hypotheses"] = []
    state["evidence_analysis"] = {}

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert report.selected_root_cause_id == ""
    assert "证据不足" in report.root_cause
    assert report.conclusion_alignment["fields"]["root_cause"]["aligned"] is True
    assert report.conclusion_alignment["fields"]["root_cause"]["claim_type"] == "insufficiency"


def test_evidence_graph_does_not_mark_unselected_hypothesis_or_link_stale_evidence(
    tmp_path,
) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"][0]["evidence_id"] = "evd-stale"
    state["gathered_evidence"][0]["raw_data"]["metadata"] = {
        "evidence_quality": {
            "status": "stale",
            "usable": False,
            "reasons": ["result_marked_stale_or_expired"],
        }
    }
    state["evidence_analysis"] = {
        "hypothesis_ranking": [
            {
                "hypothesis_id": "hyp-stale-only",
                "title": "Stale-only hypothesis",
                "category": "redis_maxclients",
                "supporting_evidence_ids": ["evd-stale"],
                "refuting_evidence_ids": [],
                "confidence": 0.8,
            }
        ]
    }

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    hypothesis_node = next(
        node for node in report.evidence_graph["nodes"] if node["node_type"] == "hypothesis"
    )
    assert report.selected_root_cause_id == ""
    assert hypothesis_node["selected"] is False
    assert not any(
        edge["source"] == "hypothesis:hyp-stale-only" and edge["relation"] == "supported_by"
        for edge in report.evidence_graph["edges"]
    )


def test_evidence_graph_does_not_guess_tool_call_when_same_tool_is_ambiguous() -> None:
    graph = build_incident_evidence_graph(
        incident_id="inc-ambiguous-tool",
        trace_id="trace-ambiguous-tool",
        root_cause="",
        selected_root_cause_id="",
        hypothesis_ranking=[],
        evidence=[
            {
                "evidence_id": "evd-ambiguous",
                "source_tool": "query_metrics",
                "step_id": "",
                "summary": "legacy metric evidence",
                "stance": "neutral",
                "raw_data": {
                    "status": "success",
                    "input_args": {"service_name": "order-service"},
                    "output": {"summary": "legacy metric evidence"},
                },
            }
        ],
        tool_calls=[
            {
                "call_id": "call-1",
                "step_id": "s1",
                "tool_name": "query_metrics",
                "input_args": {"service_name": "payment-service"},
                "status": "success",
            },
            {
                "call_id": "call-2",
                "step_id": "s2",
                "tool_name": "query_metrics",
                "input_args": {"service_name": "inventory-service"},
                "status": "success",
            },
        ],
        conclusion_alignment={},
    )

    assert not any(
        edge["source"] == "evidence:evd-ambiguous" and edge["relation"] == "produced_by"
        for edge in graph["edges"]
    )


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
        hypothesis_ranking=state["evidence_analysis"]["hypothesis_ranking"],
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
