import json

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import feedback as feedback_api, incidents
from app.models.feedback import BadCaseFeedbackCreate, DiagnosisFeedbackCreate
from app.models.trace import TraceEvent
from app.services.feedback_service import FeedbackService, classify_improvement_items
from app.services.report_generator import ReportGenerator
from scripts.eval.export_bad_cases import export_bad_cases
from tests.test_report_generator import _state_with_redis_evidence


def test_feedback_service_classifies_low_score_items(tmp_path) -> None:
    payload = DiagnosisFeedbackCreate(
        report_id="rpt-1",
        root_cause_correct="partial",
        accepted_suggestion="no",
        operator_note="缺少 Runbook 文档，慢 SQL 工具证据不足",
    )

    items = classify_improvement_items(payload)
    types = {item["type"] for item in items}

    assert "eval_case_draft" in types
    assert "tool_gap" in types
    assert "report_template_issue" in types
    assert "rag_doc_gap" in types

    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_feedback(incident_id="inc-1", payload=payload)

    assert feedback.incident_id == "inc-1"
    assert service.list_feedback(incident_id="inc-1")[0].feedback_id == feedback.feedback_id
    bad_cases = service.list_bad_cases(target="aiops", high_value_only=True)
    assert bad_cases[0].category == "hallucination_risk"
    assert bad_cases[0].evidence.metadata["incident_id"] == "inc-1"


def test_bad_case_feedback_records_rag_context_and_category(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            reason="召回到了 CPU 文档，应该命中 Redis runbook",
            expected_answer="应引用 redis_maxclients.md 并说明 connected_clients 接近上限",
            query="order-service Redis timeout 怎么排查？",
            answer="错误回答",
            citations=[],
            retrieval_results=[
                {"source_file": "cpu_high_usage.md", "chunk_id": "cpu_high_usage.md#0001"}
            ],
            rejected_results=[],
            trace_id="trace-rag-1",
            metadata={"session_id": "session-1"},
        )
    )

    assert feedback.high_value is True
    assert feedback.category == "missing_citation"
    assert feedback.evidence.query.startswith("order-service")

    raw = (tmp_path / "feedback.jsonl").read_text(encoding="utf-8")
    assert '"record_type": "bad_case"' in raw
    assert "redis_maxclients.md" in raw


def test_incident_feedback_api_records_feedback_and_bad_case(monkeypatch, tmp_path) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    report = generator.generate_from_state(
        _state_with_redis_evidence(),
        trace_events=[],
        status="completed",
    )
    feedback = FeedbackService(tmp_path / "feedback.jsonl")

    class FakeTraceService:
        def list_events(self, incident_id=None, event_type=None):
            return [
                TraceEvent(
                    trace_id=report.trace_id,
                    incident_id=report.incident_id,
                    event_type="tool_call",
                    node_name="query_metrics",
                    tool_name="query_metrics",
                    status="failed",
                    error_message="prometheus timeout",
                )
            ]

    monkeypatch.setattr(incidents, "get_report_generator", lambda: generator)
    monkeypatch.setattr(incidents, "get_feedback_service", lambda: feedback)
    monkeypatch.setattr(incidents, "get_trace_service", lambda: FakeTraceService())

    app = FastAPI()
    app.include_router(incidents.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        f"/api/incidents/{report.incident_id}/feedback",
        json={
            "report_id": report.report_id,
            "root_cause_correct": "no",
            "accepted_suggestion": "no",
            "operator_note": "报告建议没有被采纳，需要补 eval case",
            "expected_answer": "报告必须解释 Redis maxclients 和 connected_clients",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["feedback"]["incident_id"] == report.incident_id
    assert {item["type"] for item in body["feedback"]["improvement_items"]} >= {
        "eval_case_draft",
        "tool_gap",
        "report_template_issue",
    }

    list_response = client.get(f"/api/incidents/{report.incident_id}/feedback")
    assert list_response.status_code == 200
    assert len(list_response.json()["items"]) == 1
    bad_cases = feedback.list_bad_cases(target="aiops", high_value_only=True)
    assert bad_cases[0].evidence.trace_id == report.trace_id
    assert bad_cases[0].expected_answer.startswith("报告必须解释")


def test_bad_case_feedback_api_records_rag_feedback(monkeypatch, tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    monkeypatch.setattr(feedback_api, "get_feedback_service", lambda: service)

    app = FastAPI()
    app.include_router(feedback_api.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/feedback",
        json={
            "target": "rag",
            "vote": "thumb_down",
            "category": "retrieval_failure",
            "reason": "召回失败",
            "expected_answer": "应该引用 redis_maxclients.md",
            "query": "Redis timeout 怎么办？",
            "answer": "错误回答",
            "citations": [],
            "retrieval_results": [],
            "rejected_results": [{"source_file": "redis_maxclients.md"}],
            "trace_id": "trace-rag",
            "tool_calls": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()["feedback"]
    assert payload["high_value"] is True
    assert payload["category"] == "retrieval_failure"

    list_response = client.get("/api/feedback/bad-cases?target=rag&high_value_only=true")
    assert list_response.status_code == 200
    assert len(list_response.json()["items"]) == 1


def test_export_bad_cases_promotes_high_value_feedback_to_eval_yaml(tmp_path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    rag_cases = tmp_path / "rag_cases.yaml"
    aiops_cases = tmp_path / "cases.yaml"
    rag_cases.write_text("cases: []\n", encoding="utf-8")
    aiops_cases.write_text("cases: []\n", encoding="utf-8")
    service = FeedbackService(feedback_path)
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            category="retrieval_failure",
            reason="召回失败",
            expected_answer="应该引用 redis_maxclients.md，并包含 connected_clients",
            query="order-service Redis timeout 怎么排查？",
            retrieval_results=[{"source_file": "cpu_high_usage.md"}],
        )
    )
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="Prometheus 工具超时后报告没有说明降级",
            expected_answer="报告必须说明 query_metrics 失败并使用日志证据降级",
            query="payment-service 延迟升高如何诊断？",
            trace_id="trace-aiops",
            tool_calls=[
                {
                    "tool_name": "query_metrics",
                    "status": "failed",
                    "error_message": "timeout",
                },
                {"tool_name": "query_logs", "status": "success"},
            ],
            metadata={"service_name": "payment-service", "severity": "P2", "environment": "prod"},
        )
    )

    summary = export_bad_cases(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
    )

    assert summary["rag_exported_count"] == 1
    assert summary["aiops_exported_count"] == 1
    rag_payload = yaml.safe_load(rag_cases.read_text(encoding="utf-8"))
    aiops_payload = yaml.safe_load(aiops_cases.read_text(encoding="utf-8"))
    assert rag_payload["cases"][0]["expected_sources"] == ["redis_maxclients.md"]
    assert "connected_clients" in rag_payload["cases"][0]["expected_keywords"]
    assert aiops_payload["cases"][0]["expected_failed_tools"] == ["query_metrics"]
    assert aiops_payload["cases"][0]["expected_tools"] == ["query_metrics", "query_logs"]

    second_summary = export_bad_cases(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
    )
    assert second_summary["rag_exported_count"] == 0
    assert second_summary["aiops_exported_count"] == 0


def test_feedback_jsonl_keeps_runtime_context_fields(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            category="hallucination_risk",
            reason="答案脱离证据",
            expected_answer="应该拒答",
            query="未知系统如何扩容？",
            answer="无依据的扩容建议",
            citations=[{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
            retrieval_results=[{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
            rejected_results=[{"source_file": "noise.md"}],
            trace_id="trace-context",
            tool_calls=[{"tool_name": "search_runbook", "status": "success"}],
        )
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "feedback.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    payload = records[0]["payload"]
    evidence = payload["evidence"]
    assert evidence["query"] == "未知系统如何扩容？"
    assert evidence["answer"] == "无依据的扩容建议"
    assert evidence["citations"][0]["chunk_id"] == "redis.md#0001"
    assert evidence["retrieval_results"][0]["source_file"] == "redis.md"
    assert evidence["rejected_results"][0]["source_file"] == "noise.md"
    assert evidence["trace_id"] == "trace-context"
    assert evidence["tool_calls"][0]["tool_name"] == "search_runbook"
