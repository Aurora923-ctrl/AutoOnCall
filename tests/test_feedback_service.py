import json

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import feedback as feedback_api, incidents
from app.core.auth import AuthPrincipal
from app.models.feedback import BadCaseFeedbackCreate, DiagnosisFeedbackCreate
from app.models.report import DiagnosisReport
from app.models.trace import TraceEvent
from app.services.feedback_service import (
    FeedbackService,
    build_bad_case_feedback,
    build_eval_backlog_item,
    classify_improvement_items,
)
from app.services.report_generator import ReportGenerator
from scripts.eval.export_bad_cases import (
    backlog_from_eval_summary,
    build_aiops_eval_case,
    build_rag_eval_case,
    export_bad_cases,
    merge_backlog_items,
    promote_bad_cases_to_eval,
)
from tests.test_report_generator import _state_with_redis_evidence


def _mark_all_backlog_reviewed(feedback_path) -> None:
    rewritten = []
    for line in feedback_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("record_type") == "eval_backlog":
            record["payload"]["review_status"] = "reviewed"
        rewritten.append(json.dumps(record, ensure_ascii=False))
    feedback_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_feedback_service_classifies_low_score_items(tmp_path) -> None:
    payload = DiagnosisFeedbackCreate(
        report_id="rpt-1",
        root_cause_correct="partial",
        accepted_suggestion="no",
        operator_note="缺少 Runbook 文档，慢 SQL 工具证据不足",
        expected_answer="报告必须说明缺少 Runbook 和慢 SQL 工具证据",
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
    backlog = service.list_eval_backlog(target="aiops", review_status="new")
    assert backlog[0].suggested_eval_file == "eval/cases.yaml"
    assert backlog[0].review_status == "new"
    assert backlog[0].links["incident_id"] == "inc-1"


def test_positive_diagnosis_feedback_does_not_pollute_bad_case_backlog(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")

    feedback = service.submit_feedback(
        incident_id="inc-ok",
        payload=DiagnosisFeedbackCreate(
            report_id="rpt-ok",
            root_cause_correct="yes",
            accepted_suggestion="yes",
            operator_note="诊断结论和处置建议都已采纳",
        ),
    )

    assert feedback.root_cause_correct == "yes"
    assert service.list_bad_cases() == []
    assert service.list_eval_backlog() == []


def test_positive_diagnosis_feedback_with_no_problem_wording_stays_positive(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")

    service.submit_feedback(
        incident_id="inc-ok",
        payload=DiagnosisFeedbackCreate(
            report_id="rpt-ok",
            root_cause_correct="yes",
            accepted_suggestion="yes",
            operator_note="没有问题，诊断和处置都已采纳",
        ),
    )

    assert service.list_bad_cases() == []
    assert service.list_eval_backlog() == []


def test_weak_negative_diagnosis_feedback_does_not_enter_bad_case_pool(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")

    service.submit_feedback(
        incident_id="inc-weak",
        payload=DiagnosisFeedbackCreate(
            report_id="rpt-weak",
            root_cause_correct="partial",
            accepted_suggestion="yes",
            operator_note="感觉还可以再优化",
        ),
    )

    assert service.list_feedback(incident_id="inc-weak")
    assert service.list_bad_cases() == []
    assert service.list_eval_backlog() == []


def test_weak_negative_diagnosis_feedback_with_report_does_not_enter_backlog(tmp_path) -> None:
    generator = ReportGenerator(tmp_path / "reports.db")
    report = generator.generate_from_state(
        _state_with_redis_evidence(),
        trace_events=[],
        status="completed",
    )
    service = FeedbackService(tmp_path / "feedback.jsonl")

    service.submit_feedback(
        incident_id=report.incident_id,
        payload=DiagnosisFeedbackCreate(
            report_id=report.report_id,
            root_cause_correct="partial",
            accepted_suggestion="yes",
            operator_note="感觉还可以再优化",
        ),
        report=report,
        trace_events=[],
    )

    assert service.list_feedback(incident_id=report.incident_id)
    assert service.list_bad_cases() == []
    assert service.list_eval_backlog() == []


def test_diagnosis_feedback_rejects_mismatched_report_context(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    report = DiagnosisReport(
        incident_id="inc-real",
        report_id="rpt-real",
        trace_id="trace-real",
    )
    payload = DiagnosisFeedbackCreate(
        report_id="rpt-other",
        root_cause_correct="no",
        accepted_suggestion="no",
    )

    with pytest.raises(ValueError, match="report_id"):
        service.submit_feedback(
            incident_id="inc-real",
            payload=payload,
            report=report,
        )


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
    backlog = service.list_eval_backlog(target="rag")
    assert backlog[0].category == "missing_citation"
    assert backlog[0].suggested_eval_dimension == "rag_citation_coverage"
    assert backlog[0].suggested_eval_suite == "rag"

    raw = (tmp_path / "feedback.jsonl").read_text(encoding="utf-8")
    assert '"record_type": "bad_case"' in raw
    assert '"record_type": "eval_backlog"' in raw
    assert "redis_maxclients.md" in raw


def test_eval_backlog_item_redacts_and_truncates_runtime_snapshot(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="query_metrics failed",
            expected_answer="report should show degraded metrics path",
            query="payment-service latency spike",
            answer="x" * 2000,
            tool_calls=[
                {
                    "tool_name": "query_metrics",
                    "status": "failed",
                    "error_message": "token=secret-value timeout",
                    "input_args": {"password": "secret"},
                    "output": {"summary": "failed", "artifact_id": "art-1"},
                }
            ],
            metadata={"incident_id": "inc-1", "api_token": "secret-token"},
        )
    )

    item = build_eval_backlog_item(feedback)

    assert item.priority == "P0"
    assert item.suggested_eval_file == "eval/cases.yaml"
    snapshot = item.evidence_snapshot
    assert len(snapshot["answer_preview"]) == 1200
    assert "secret-value" not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["tool_calls"][0]["artifact_id"] == "art-1"


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
    assert len(bad_cases[0].evidence.answer) <= 12000
    assert "full diagnosis report is available by report_id" in bad_cases[0].evidence.answer
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

    backlog_response = client.get("/api/feedback/eval-backlog?target=rag")
    assert backlog_response.status_code == 200
    backlog_payload = backlog_response.json()
    assert backlog_payload["summary"]["total"] == 1
    assert backlog_payload["items"][0]["suggested_eval_file"] == "eval/rag_cases.yaml"


def test_export_bad_cases_defaults_to_reviewable_backlog_without_mutating_eval_yaml(
    tmp_path,
) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    backlog_path = tmp_path / "eval_backlog_drafts.json"
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
        backlog_path=backlog_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
    )

    assert summary["backlog_count"] == 2
    assert summary["rag_exported_count"] == 0
    assert summary["aiops_exported_count"] == 0
    assert summary["promote_to_eval"] is False
    backlog_payload = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert backlog_payload["summary"]["by_target"] == {"aiops": 1, "rag": 1}
    rag_payload = yaml.safe_load(rag_cases.read_text(encoding="utf-8"))
    aiops_payload = yaml.safe_load(aiops_cases.read_text(encoding="utf-8"))
    assert rag_payload["cases"] == []
    assert aiops_payload["cases"] == []


def test_promote_bad_cases_to_eval_yaml_is_explicit(tmp_path) -> None:
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

    _mark_all_backlog_reviewed(feedback_path)
    summary = promote_bad_cases_to_eval(
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

    second_summary = promote_bad_cases_to_eval(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
    )
    assert second_summary["rag_exported_count"] == 0
    assert second_summary["aiops_exported_count"] == 0


def test_backlog_from_eval_summary_captures_failed_aiops_and_rag_cases(tmp_path) -> None:
    summary_path = tmp_path / "eval_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "summary": {
                    "failed_cases": [
                        {
                            "suite": "aiops",
                            "id": "redis_forbidden_restart",
                            "failed_metrics": ["forbidden_precision"],
                            "failure_reasons": {"forbidden_precision": "not blocked"},
                        },
                        {
                            "suite": "rag",
                            "id": "redis_doc_recall",
                            "failed_metrics": ["recall_at_k"],
                            "failure_reasons": {"recall_at_k": "missed expected source"},
                            "expected_sources": ["redis_postmortem.pdf"],
                            "retrieved_sources": ["cpu_high_usage.md"],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    items = backlog_from_eval_summary(summary_path)

    assert [item.target for item in items] == ["aiops", "rag"]
    assert items[0].category == "permission_denied"
    assert items[0].priority == "P0"
    assert items[1].suggested_eval_file == "eval/rag_cases.yaml"
    assert items[1].evidence_snapshot["expected_sources"] == ["redis_postmortem.pdf"]


def test_backlog_from_eval_summary_routes_ragas_and_change_failures(tmp_path) -> None:
    ragas_summary = tmp_path / "ragas_summary.json"
    ragas_summary.write_text(
        json.dumps(
            {
                "run": {
                    "suite": "ragas",
                    "evaluation_scope": "optional RAGAS quality regression",
                },
                "case_scores": [
                    {
                        "id": "redis_ragas_quality",
                        "passed": False,
                        "failed_metrics": ["id_based_context_recall"],
                        "failure_reasons": {"id_based_context_recall": "missed context id"},
                        "tags": ["core_interview"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    change_summary = tmp_path / "change_summary.json"
    change_summary.write_text(
        json.dumps(
            {
                "run": {"suite": "change", "evaluation_scope": "safe change regression"},
                "summary": {
                    "failed_cases": [
                        {
                            "id": "mysql_safe_change_missing_rollback",
                            "failed_metrics": ["rollback_recommendation_rate"],
                            "failure_reasons": {"rollback_recommendation_rate": "rollback missing"},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    ragas_items = backlog_from_eval_summary(ragas_summary)
    change_items = backlog_from_eval_summary(change_summary)

    assert ragas_items[0].target == "ragas"
    assert ragas_items[0].suggested_eval_suite == "ragas"
    assert ragas_items[0].suggested_eval_file == "eval/ragas_cases.review.json"
    assert ragas_items[0].category == "retrieval_failure"
    assert ragas_items[0].evidence_snapshot["ragas_tags"] == ["core_interview"]
    assert "not live adapter facts" in ragas_items[0].metadata["quality_boundary"]
    assert "skip_rag_yaml" in ragas_items[0].metadata["promotion_policy"]
    assert change_items[0].target == "change"
    assert change_items[0].suggested_eval_suite == "change"
    assert change_items[0].suggested_eval_file == "eval/change_cases.yaml"
    assert change_items[0].category == "tool_failure"
    assert change_items[0].suggested_eval_dimension == "safe_change_regression_gate"
    assert "safe-change" in change_items[0].expected_behavior.lower()


def test_backlog_from_eval_summary_inherits_parent_run_provenance(tmp_path) -> None:
    summary_path = tmp_path / "eval_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run": {
                    "run_id": "run-1",
                    "dataset": {"sha256": "dataset-sha"},
                    "environment": {
                        "evaluation_fingerprint": "eval-fingerprint",
                        "execution_identity": {
                            "actual_model": "actual-model",
                            "actual_embedding_model": "actual-embedding",
                        },
                    },
                },
                "summary": {
                    "failed_cases": [
                        {"suite": "aiops", "id": "case-1", "failed_metrics": ["tool_hit"]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    item = backlog_from_eval_summary(summary_path)[0]
    provenance = item.metadata["provenance"]

    assert provenance["run_id"] == "run-1"
    assert provenance["model"] == "actual-model"
    assert provenance["embedding_model"] == "actual-embedding"
    assert provenance["dataset"]["sha256"] == "dataset-sha"
    assert provenance["evaluation_fingerprint"] == "eval-fingerprint"
    assert provenance["artifact"] == "eval_summary.json"


def test_direct_feedback_marks_invalid_references_as_orphaned(tmp_path) -> None:
    class EmptyStore:
        def get_incident_state(self, incident_id):
            return None

        def get_latest_report(self, incident_id):
            return None

        def get_aiops_session_snapshot(self, session_id):
            return None

        def list_trace_events(self, *, incident_id=None, trace_id=None, event_type=None):
            return []

    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="invalid runtime link",
            expected_answer="keep the link traceable",
            query="incident",
            metadata={
                "incident_id": "missing-incident",
                "report_id": "missing-report",
                "session_id": "missing-session",
                "run_id": "run-1",
            },
        ),
        reference_store=EmptyStore(),
    )

    assert feedback.reference_status == "orphaned"
    assert "incident_not_found" in feedback.orphan_reasons
    assert service.list_eval_backlog() == []


def test_direct_feedback_rejects_trace_that_does_not_match_report(tmp_path) -> None:
    class Store:
        def get_incident_state(self, incident_id):
            return object()

        def get_report(self, report_id):
            return type("Report", (), {"incident_id": "inc-1", "trace_id": "trace-real"})()

        def get_aiops_session_snapshot(self, session_id):
            return None

        def list_trace_events(self, *, incident_id=None, trace_id=None, event_type=None):
            return [object()]

    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="wrong trace",
            expected_answer="preserve report identity",
            query="incident",
            trace_id="trace-other",
            metadata={"incident_id": "inc-1", "report_id": "rpt-1"},
        ),
        reference_store=Store(),
    )

    assert feedback.reference_status == "orphaned"
    assert "trace_report_mismatch" in feedback.orphan_reasons


def test_reference_refresh_rejects_existing_backlog_and_preserves_corrupt_lines(tmp_path) -> None:
    class ToggleStore:
        available = True

        def get_incident_state(self, incident_id):
            return object() if self.available else None

        def get_report(self, report_id):
            if not self.available:
                return None
            return type("Report", (), {"incident_id": "inc-1"})()

        def get_aiops_session_snapshot(self, session_id):
            if not self.available:
                return None
            return type("Snapshot", (), {"incident_id": "inc-1"})()

        def list_trace_events(self, *, incident_id=None, trace_id=None, event_type=None):
            return [object()] if self.available else []

    path = tmp_path / "feedback.jsonl"
    store = ToggleStore()
    service = FeedbackService(path)
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="runtime failure",
            expected_answer="preserve the regression case",
            query="incident",
            trace_id="trace-1",
            metadata={
                "incident_id": "inc-1",
                "report_id": "rpt-1",
                "session_id": "run-1",
                "run_id": "run-1",
            },
        ),
        reference_store=store,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{corrupt-json\n")

    store.available = False
    refreshed = service.list_bad_cases(reference_store=store)

    assert refreshed[0].reference_status == "orphaned"
    assert service.list_eval_backlog()[0].review_status == "rejected"
    assert "{corrupt-json" in path.read_text(encoding="utf-8")


def test_historical_report_reference_is_verified_when_report_still_exists(tmp_path) -> None:
    class HistoricalReportStore:
        def get_incident_state(self, incident_id):
            return object()

        def get_report(self, report_id):
            return type("Report", (), {"incident_id": "inc-1"})()

        def get_aiops_session_snapshot(self, session_id):
            return None

        def list_trace_events(self, *, incident_id=None, trace_id=None, event_type=None):
            return []

    service = FeedbackService(tmp_path / "feedback.jsonl")
    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            reason="historical report feedback",
            expected_answer="historical reports remain traceable",
            query="incident",
            metadata={"incident_id": "inc-1", "report_id": "rpt-old"},
        ),
        reference_store=HistoricalReportStore(),
    )

    assert feedback.reference_status == "verified"


def test_direct_ragas_and_change_feedback_routes_without_mixing_rag_promotion(tmp_path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    service = FeedbackService(feedback_path)
    ragas_feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="ragas",
            vote="thumb_down",
            category="missing_citation",
            reason="RAGAS id recall failed",
            expected_answer="RAGAS should preserve context ids and citations",
            query="Redis maxclients 怎么处理？",
            metadata={"suite": "ragas"},
        )
    )
    change_feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="change",
            vote="thumb_down",
            category="tool_failure",
            reason="safe change 缺少 rollback",
            expected_answer="必须包含 rollback 和 dry-run",
            query="Redis maxclients safe change",
            metadata={
                "suite": "change",
                "incident_id": "inc-change",
                "observe_metrics": ["redis_connected_clients"],
            },
        )
    )

    ragas_item = build_eval_backlog_item(ragas_feedback)
    change_item = build_eval_backlog_item(change_feedback)
    assert ragas_item.suggested_eval_suite == "ragas"
    assert ragas_item.suggested_eval_dimension == "ragas_answer_quality_gate"
    assert "not live adapter facts" in ragas_item.metadata["quality_boundary"]
    assert "skip_rag_yaml" in ragas_item.metadata["promotion_policy"]
    assert change_item.suggested_eval_suite == "change"
    assert change_item.suggested_eval_file == "eval/change_cases.yaml"

    rag_cases = tmp_path / "rag_cases.yaml"
    aiops_cases = tmp_path / "cases.yaml"
    change_cases = tmp_path / "change_cases.yaml"
    for path in [rag_cases, aiops_cases, change_cases]:
        path.write_text("cases: []\n", encoding="utf-8")
    summary = promote_bad_cases_to_eval(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
        change_cases_path=change_cases,
    )

    assert summary["rag_exported_count"] == 0
    assert summary["change_exported_count"] == 0
    assert summary["ragas_skipped_count"] == 0
    assert yaml.safe_load(rag_cases.read_text(encoding="utf-8"))["cases"] == []
    assert yaml.safe_load(change_cases.read_text(encoding="utf-8"))["cases"] == []


@pytest.mark.parametrize(
    ("target", "metric", "expected_category"),
    [
        ("ragas", "id_based_context_recall", "retrieval_failure"),
        ("ragas", "id_based_context_precision", "retrieval_failure"),
        ("ragas", "citation_grounding_hit", "retrieval_failure"),
        ("ragas", "oncall_actionability_score", "poor_report_quality"),
        ("ragas", "answer_relevancy", "hallucination_risk"),
        ("ragas", "incident_boundary_hit", "hallucination_risk"),
        ("ragas", "confusion_disambiguation_hit", "hallucination_risk"),
        ("ragas", "refusal_boundary", "hallucination_risk"),
        ("change", "forbidden_change_block_rate", "permission_denied"),
        ("change", "precheck_recall", "tool_failure"),
        ("change", "rollback_recommendation_rate", "tool_failure"),
        ("change", "change_plan_completeness", "tool_failure"),
    ],
)
def test_direct_feedback_infers_ragas_and_change_categories_from_real_metrics(
    tmp_path,
    target,
    metric,
    expected_category,
) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")

    feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target=target,
            vote="thumb_down",
            reason=f"{metric} failed",
            expected_answer="must preserve quality gate",
            query="quality regression",
            metadata={"suite": target, "failed_metrics": [metric]},
        )
    )

    assert feedback.category == expected_category


def test_unreviewed_bad_cases_are_not_promoted_to_eval_yaml(tmp_path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    service = FeedbackService(feedback_path)
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            category="retrieval_failure",
            reason="召回失败",
            expected_answer="应该引用 redis_maxclients.md",
            query="Redis timeout 怎么办？",
        )
    )
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="tool_failure",
            reason="query_metrics failed",
            expected_answer="报告必须说明工具失败",
            query="payment-service latency",
            tool_calls=[{"tool_name": "query_metrics", "status": "failed"}],
        )
    )
    rag_cases = tmp_path / "rag_cases.yaml"
    aiops_cases = tmp_path / "cases.yaml"
    change_cases = tmp_path / "change_cases.yaml"
    for path in [rag_cases, aiops_cases, change_cases]:
        path.write_text("cases: []\n", encoding="utf-8")

    summary = promote_bad_cases_to_eval(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
        change_cases_path=change_cases,
    )

    assert summary["rag_exported_count"] == 0
    assert summary["aiops_exported_count"] == 0
    assert yaml.safe_load(rag_cases.read_text(encoding="utf-8"))["cases"] == []
    assert yaml.safe_load(aiops_cases.read_text(encoding="utf-8"))["cases"] == []


def test_promote_requires_reviewed_backlog_and_skips_ragas_yaml(tmp_path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    service = FeedbackService(feedback_path)
    ragas_feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="ragas",
            vote="thumb_down",
            category="missing_citation",
            reason="RAGAS citation grounding failed",
            expected_answer="Preserve RAGAS answer-quality gates",
            query="Redis maxclients 怎么处理？",
            metadata={"suite": "ragas"},
        )
    )
    change_feedback = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="change",
            vote="thumb_down",
            category="tool_failure",
            reason="缺少 rollback",
            expected_answer="必须包含 rollback",
            query="Redis maxclients safe change",
            metadata={"suite": "change"},
        )
    )
    records = [
        json.loads(line)
        for line in feedback_path.read_text(encoding="utf-8").splitlines()
        if '"record_type": "eval_backlog"' in line
    ]
    rewritten = []
    for line in feedback_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("record_type") == "eval_backlog":
            if record["payload"]["feedback_id"] in {
                ragas_feedback.feedback_id,
                change_feedback.feedback_id,
            }:
                record["payload"]["review_status"] = "reviewed"
        rewritten.append(json.dumps(record, ensure_ascii=False))
    assert records
    feedback_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    rag_cases = tmp_path / "rag_cases.yaml"
    aiops_cases = tmp_path / "cases.yaml"
    change_cases = tmp_path / "change_cases.yaml"
    for path in [rag_cases, aiops_cases, change_cases]:
        path.write_text("cases: []\n", encoding="utf-8")

    summary = promote_bad_cases_to_eval(
        feedback_path=feedback_path,
        rag_cases_path=rag_cases,
        aiops_cases_path=aiops_cases,
        change_cases_path=change_cases,
    )

    assert summary["ragas_skipped_count"] == 1
    assert "answer-quality fixture draft" in summary["ragas_promotion_note"]
    assert summary["change_exported_count"] == 1
    assert yaml.safe_load(rag_cases.read_text(encoding="utf-8"))["cases"] == []
    assert len(yaml.safe_load(change_cases.read_text(encoding="utf-8"))["cases"]) == 1


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


def test_direct_feedback_upsert_is_scoped_by_owner_and_rebuilds_backlog(tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    first = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            idempotency_key="message-1",
            category="retrieval_failure",
            reason="wrong source",
            expected_answer="use redis.md",
            query="redis timeout",
        ),
        owner_id="alice",
    )
    updated = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_up",
            idempotency_key="message-1",
            reason="fixed",
            query="redis timeout",
        ),
        owner_id="alice",
    )
    other_owner = service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            idempotency_key="message-1",
            category="retrieval_failure",
            reason="still wrong",
            expected_answer="use redis.md",
            query="redis timeout",
        ),
        owner_id="bob",
    )

    assert updated.feedback_id == first.feedback_id
    assert other_owner.feedback_id != first.feedback_id
    assert service.list_bad_cases(owner_id="alice")[0].vote == "thumb_up"
    assert service.list_eval_backlog(owner_id="alice") == []
    assert len(service.list_eval_backlog(owner_id="bob")) == 1


def test_merge_backlog_preserves_review_state_and_richer_snapshot() -> None:
    base = build_eval_backlog_item(
        build_bad_case_feedback(
            BadCaseFeedbackCreate(
                target="aiops",
                vote="thumb_down",
                category="tool_failure",
                reason="metrics failed",
                expected_answer="degrade to logs",
                query="latency spike",
                tool_calls=[{"tool_name": "query_metrics", "status": "failed"}],
            )
        )
    )
    reviewed = base.model_copy(
        update={
            "review_status": "reviewed",
            "reviewed_by": "alice",
            "failure_reasons": ["reviewed failure"],
            "evidence_snapshot": {"query": "latency spike", "review_note": "keep this"},
        }
    )
    regenerated = build_eval_backlog_item(
        build_bad_case_feedback(
            BadCaseFeedbackCreate(
                target="aiops",
                vote="thumb_down",
                category="tool_failure",
                reason="metrics and logs failed",
                expected_answer="degrade with explicit failure evidence",
                query="latency spike",
                tool_calls=[
                    {"tool_name": "query_metrics", "status": "failed"},
                    {"tool_name": "query_logs", "status": "failed"},
                ],
            )
        )
    ).model_copy(
        update={
            "backlog_id": reviewed.backlog_id,
            "feedback_id": reviewed.feedback_id,
            "suggested_eval_case_id": reviewed.suggested_eval_case_id,
        }
    )

    merged = merge_backlog_items([reviewed, regenerated])

    assert len(merged) == 1
    assert merged[0].review_status == "reviewed"
    assert merged[0].reviewed_by == "alice"
    assert merged[0].evidence_snapshot["review_note"] == "keep this"
    assert "reviewed failure" in merged[0].failure_reasons


def test_exported_cases_redact_secrets_and_neutralize_formula_cells() -> None:
    rag_case = build_rag_eval_case(
        build_bad_case_feedback(
            BadCaseFeedbackCreate(
                target="rag",
                vote="thumb_down",
                category="retrieval_failure",
                reason='=HYPERLINK("https://evil.invalid")',
                expected_answer="token=secret-value use redis.md",
                query="+SUM(1,1)",
            )
        )
    )
    aiops_case = build_aiops_eval_case(
        build_bad_case_feedback(
            BadCaseFeedbackCreate(
                target="aiops",
                vote="thumb_down",
                category="permission_denied",
                reason="@cmd",
                expected_answer="block the action",
                query="-2+3",
            )
        )
    )

    serialized = json.dumps([rag_case, aiops_case], ensure_ascii=False)
    assert "secret-value" not in serialized
    assert rag_case is not None
    assert rag_case["query"].startswith("'")
    assert rag_case["feedback"]["reason"].startswith("'")
    assert aiops_case is not None
    assert aiops_case["input"].startswith("'")
    assert aiops_case["feedback"]["reason"].startswith("'")
    assert aiops_case["expected_risk_policy"] == "forbidden"
    assert aiops_case["expected_report_status"] == "blocked"


def test_feedback_identifiers_reject_log_control_characters() -> None:
    with pytest.raises(ValueError, match="control characters"):
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            idempotency_key="message\nforged",
        )
    with pytest.raises(ValueError, match="control characters"):
        BadCaseFeedbackCreate(
            target="rag",
            vote="thumb_down",
            trace_id="trace\tforged",
        )
    with pytest.raises(ValueError, match="metadata.incident_id"):
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            metadata={"incident_id": "inc\nforged"},
        )
    with pytest.raises(ValueError, match="report_id"):
        DiagnosisFeedbackCreate(
            report_id="rpt\nforged",
            root_cause_correct="no",
            accepted_suggestion="no",
        )


def test_feedback_api_uses_principal_id_not_shared_display_name(monkeypatch, tmp_path) -> None:
    service = FeedbackService(tmp_path / "feedback.jsonl")
    monkeypatch.setattr(feedback_api, "get_feedback_service", lambda: service)
    monkeypatch.setattr(feedback_api, "create_aiops_store", lambda: None)

    alice = AuthPrincipal(
        enabled=True,
        token_name="shared-actor",
        principal_id="principal-alice",
        scopes=frozenset({"read", "diagnose"}),
    )
    bob = AuthPrincipal(
        enabled=True,
        token_name="shared-actor",
        principal_id="principal-bob",
        scopes=frozenset({"read", "diagnose"}),
    )

    async def alice_dependency():
        return alice

    async def bob_dependency():
        return bob

    app = FastAPI()
    app.include_router(feedback_api.router, prefix="/api")
    dependency = next(
        route.dependant.dependencies[0].call
        for route in app.routes
        if getattr(route, "path", "") == "/api/feedback"
    )
    app.dependency_overrides[dependency] = alice_dependency
    client = TestClient(app)
    payload = {
        "target": "rag",
        "vote": "thumb_down",
        "idempotency_key": "message-1",
        "category": "retrieval_failure",
        "reason": "wrong source",
        "expected_answer": "use redis.md",
        "query": "redis timeout",
    }
    assert client.post("/api/feedback", json=payload).status_code == 200

    app.dependency_overrides[dependency] = bob_dependency
    assert client.post("/api/feedback", json=payload).status_code == 200

    assert len(service.list_bad_cases(owner_id="principal-alice")) == 1
    assert len(service.list_bad_cases(owner_id="principal-bob")) == 1


def test_backlog_listing_refreshes_orphaned_references(tmp_path) -> None:
    class ToggleStore:
        available = True

        def get_incident_state(self, incident_id):
            return object() if self.available else None

        def get_report(self, report_id):
            if not self.available:
                return None
            return type("Report", (), {"incident_id": "inc-1"})()

        def get_aiops_session_snapshot(self, session_id):
            return None

        def list_trace_events(self, *, incident_id=None, trace_id=None, event_type=None):
            return []

    store = ToggleStore()
    service = FeedbackService(tmp_path / "feedback.jsonl")
    service.submit_bad_case_feedback(
        BadCaseFeedbackCreate(
            target="aiops",
            vote="thumb_down",
            category="poor_report_quality",
            reason="report is incomplete",
            expected_answer="include evidence",
            query="incident",
            metadata={"incident_id": "inc-1", "report_id": "rpt-1"},
        ),
        owner_id="alice",
        reference_store=store,
    )

    store.available = False
    items = service.list_eval_backlog(owner_id="alice", reference_store=store)

    assert len(items) == 1
    assert items[0].review_status == "rejected"
