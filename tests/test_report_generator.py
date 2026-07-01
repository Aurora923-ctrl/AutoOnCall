"""Tests for deterministic AIOps diagnosis report generation."""

import importlib

import pytest
from fastapi import HTTPException

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.models.evidence import Evidence
from app.models.trace import ToolCallRecord
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
        ).model_dump(mode="json")
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
        ).model_dump(mode="json")
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
    assert "关键证据" in report.markdown
    assert "## 已确认事实" in report.markdown
    assert "## 推断结论" in report.markdown
    assert "## 根因假设矩阵" in report.markdown
    assert "## 下一步建议" in report.markdown
    assert "## 证据质量" in report.markdown
    assert "## 运行告警" in report.markdown
    assert report.warnings == state["warnings"]
    assert state["warnings"][0] in report.uncertainties
    assert "type=redis" in report.markdown
    assert "source=" in report.markdown
    assert "置信度原因" in report.markdown
    assert report.confidence > 0.7

    reloaded = ReportGenerator(tmp_path / "reports.db")
    assert reloaded.get_report(report.incident_id).report_id == report.report_id


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

    assert "## Runbook 引用" in report.markdown
    assert "cpu_high_usage.md" in report.markdown
    assert "chunk=cpu_high_usage.md#0001" in report.markdown
    assert "score=0.2000" in report.markdown


def test_report_generator_exposes_tracing_and_redpanda_dependency_signals(tmp_path) -> None:
    state = _state_with_redis_evidence()
    state["gathered_evidence"].extend(
        [
            Evidence(
                source_tool="query_traces",
                step_id="s2",
                summary="Jaeger 返回 order-service 最近 3 条 trace，error_spans=1, slowest_us=3200000",
                evidence_type="trace",
                data_source="jaeger",
                stance="supporting",
                confidence_reason="Tracing 后端返回调用链耗时和错误 span 信号",
                fact="trace_count=3 error_span_count=1",
                raw_data={"status": "success", "output": {"source": "jaeger"}},
                confidence=0.82,
            ).model_dump(mode="json"),
            Evidence(
                source_tool="query_message_queue_status",
                step_id="s3",
                summary="Redpanda ready，topics=2, matched_partitions=1",
                evidence_type="message_queue",
                data_source="redpanda",
                stance="supporting",
                confidence_reason="消息队列后端返回 topic/partition 状态",
                fact="topic_count=2 matched_partition_count=1",
                raw_data={"status": "success", "output": {"source": "redpanda"}},
                confidence=0.82,
            ).model_dump(mode="json"),
        ]
    )
    state["tool_call_records"].extend(
        [
            ToolCallRecord(
                trace_id=state["trace_id"],
                incident_id=state["incident"]["incident_id"],
                step_id="s2",
                tool_name="query_traces",
                input_args={"service_name": "order-service", "lookback": "1h", "limit": 20},
                output={"summary": "Jaeger 返回 order-service 最近 3 条 trace"},
                output_summary="Jaeger 返回 order-service 最近 3 条 trace，error_spans=1",
                data_source="jaeger",
                latency_ms=21,
                status="success",
            ).model_dump(mode="json"),
            ToolCallRecord(
                trace_id=state["trace_id"],
                incident_id=state["incident"]["incident_id"],
                step_id="s3",
                tool_name="query_message_queue_status",
                input_args={"service_name": "order-service", "topic": "redpanda-order"},
                output={"summary": "Redpanda ready，topics=2, matched_partitions=1"},
                output_summary="Redpanda ready，topics=2, matched_partitions=1",
                data_source="redpanda",
                latency_ms=19,
                status="success",
            ).model_dump(mode="json"),
        ]
    )

    report = ReportGenerator(tmp_path / "reports.db").generate_from_state(
        state,
        trace_events=[],
        status="completed",
    )

    assert [item["domain"] for item in report.dependency_signals] == [
        "tracing",
        "message_queue",
    ]
    assert report.dependency_signals[0]["backend"] == "jaeger"
    assert report.dependency_signals[1]["backend"] == "redpanda"
    assert "## Tracing 与消息队列证据" in report.markdown
    assert "backend=jaeger" in report.markdown
    assert "backend=redpanda" in report.markdown


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
    assert "等待人工审批" in report.markdown
    assert "审批动作：调整 Redis maxclients 配置" in report.markdown
    assert "## 人工动作与回滚边界" in report.markdown
    assert "## 变更计划草案" in report.markdown
    assert "人工调整配置" in report.markdown
    assert "Agent 只输出诊断和处置建议" in report.markdown
    assert "回滚方案" in report.markdown


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
