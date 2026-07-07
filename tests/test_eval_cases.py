"""Tests for offline AIOps evaluation cases."""

import os
from copy import deepcopy

import pytest

from app.agent.aiops.executor import (
    _tool_result_to_call_record as executor_tool_result_to_call_record,
    _tool_result_to_evidence as executor_tool_result_to_evidence,
)
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.tools.base import ToolExecutionResult
from scripts.eval.eval_cases import (
    METRIC_NAMES,
    approval_boundary_hit,
    evaluate_cases,
    load_cases,
    load_env_file,
    render_markdown_summary,
    render_summary,
    required_live_sources_hit,
    runtime_vs_incident_boundary_hit,
    tool_result_to_call_record,
    tool_result_to_evidence,
    write_eval_artifacts,
)


def test_eval_cases_yaml_contains_expected_scenarios() -> None:
    cases = load_cases("eval/cases.yaml")
    case_ids = {case["id"] for case in cases}

    assert "redis_maxclients_timeout" in case_ids
    assert "mysql_slow_query_latency" in case_ids
    assert "pod_crashloop" in case_ids
    assert "service_5xx_unavailable" in case_ids
    assert "slow_response_dependency_timeout" in case_ids
    assert "cpu_high_usage_spike" in case_ids
    assert "memory_oom_pressure" in case_ids
    assert "disk_no_space_write_failure" in case_ids
    assert "restart_service_requires_approval" in case_ids
    assert "forbidden_delete_pod" in case_ids
    assert "forbidden_unaudited_sql" in case_ids
    assert "logs_timeout_graceful_degradation" in case_ids
    assert "metrics_timeout_redis_degradation" in case_ids
    assert "k8s_permission_denied_incomplete_report" in case_ids
    assert "redis_log_status_conflict" in case_ids
    assert "runbook_no_answer_rejection" in case_ids
    assert len(cases) == 16

    for case in cases:
        assert case.get("expected_tools")
        assert case.get("expected_executed_tools")
        assert case.get("forbidden_tools")
        assert case.get("expected_report_status")

    mysql_case = next(case for case in cases if case["id"] == "mysql_slow_query_latency")
    mysql_labels = mysql_case["alertmanager_payload"]["alerts"][0]["labels"]
    assert mysql_labels["mysql_instance"] == "payment-mysql"
    assert mysql_case["golden"]["required_live_sources"]["query_mysql_status"] == "mysql"

    redis_case = next(case for case in cases if case["id"] == "redis_maxclients_timeout")
    assert redis_case["golden"]["required_live_sources"]["query_redis_status"] == "redis_info"


def test_eval_env_file_loader_sets_values_before_live_registry(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / "sandbox.env"
    env_file.write_text("REDIS_URL=redis://127.0.0.1:16379/0\n", encoding="utf-8")

    load_env_file(env_file)

    assert os.environ["REDIS_URL"] == "redis://127.0.0.1:16379/0"
    os.environ.pop("REDIS_URL", None)


def test_required_live_sources_hit_rejects_fixture_sources() -> None:
    good = ToolCallRecord(
        trace_id="trace-live",
        incident_id="inc-live",
        step_id="s1",
        tool_name="query_mysql_status",
        output={"source": "mysql"},
        data_source="mysql",
        status="success",
    )
    bad = good.model_copy(update={"output": {"source": "eval_fixture"}})
    golden = {"required_live_sources": {"query_mysql_status": "mysql"}}

    assert required_live_sources_hit([good], golden) is True
    assert required_live_sources_hit([bad], golden) is False


def test_golden_boundary_metrics_require_runtime_and_approval_explanations() -> None:
    redis_record = ToolCallRecord(
        trace_id="trace-live",
        incident_id="inc-live",
        step_id="s1",
        tool_name="query_redis_status",
        output={
            "source": "redis_info",
            "incident_evidence": {"connected_clients": 9940},
            "live_info": {"connected_clients": 1},
            "uncertainty": "Current Redis runtime is not actually saturated.",
        },
        data_source="redis_info",
        status="success",
    )
    report = type(
        "Report",
        (),
        {
            "markdown": "## 数据源边界\nincident_evidence vs live_info; not actually saturated.",
            "remediation_suggestion": "Changing Redis maxclients requires human approval.",
            "risk_summary": {},
            "approval_status": "not_required",
        },
    )()
    golden = {
        "diagnosis_needs_approval": False,
        "remediation_change_requires_approval": True,
        "required_output_signals": {"query_redis_status": {}},
    }

    assert runtime_vs_incident_boundary_hit(report, [redis_record], golden) is True
    assert approval_boundary_hit(report, golden, "allow") is True

    weak_report = type(
        "Report",
        (),
        {
            "markdown": "Redis connected_clients high.",
            "remediation_suggestion": "Increase maxclients.",
            "risk_summary": {},
            "approval_status": "not_required",
        },
    )()
    weak_record = redis_record.model_copy(
        update={"output": {"source": "redis_info", "summary": "Redis connected_clients high."}}
    )
    assert runtime_vs_incident_boundary_hit(weak_report, [weak_record], golden) is False
    assert approval_boundary_hit(weak_report, golden, "allow") is False


def test_eval_tool_result_conversion_matches_executor_schema() -> None:
    step = PlanStep(
        step_id="s1",
        tool_name="query_metrics",
        purpose="检查指标",
        input_args={"service_name": "order-service"},
        expected_evidence="指标证据",
    )
    result = ToolExecutionResult(
        tool_name="query_metrics",
        status="success",
        input_args=step.input_args,
        output={"summary": "P95 latency high", "p95_latency_ms": {"status": "high"}},
        latency_ms=12.5,
    )
    state = {"trace_id": "trace-eval", "incident": {"incident_id": "inc-eval"}}

    eval_evidence = tool_result_to_evidence(result, step).model_dump(mode="json")
    executor_evidence = executor_tool_result_to_evidence(result, step).model_dump(mode="json")
    eval_record = tool_result_to_call_record(
        result,
        step,
        trace_id="trace-eval",
        incident_id="inc-eval",
    ).model_dump(mode="json")
    executor_record = executor_tool_result_to_call_record(
        result,
        step,
        state,
    ).model_dump(mode="json")

    for volatile_key in ("evidence_id", "created_at"):
        eval_evidence.pop(volatile_key, None)
        executor_evidence.pop(volatile_key, None)
    for volatile_key in ("call_id", "created_at"):
        eval_record.pop(volatile_key, None)
        executor_record.pop(volatile_key, None)

    assert eval_evidence == executor_evidence
    assert eval_record == executor_record


@pytest.mark.asyncio
async def test_eval_cases_all_pass_with_offline_fallbacks(tmp_path) -> None:
    load_env_file("deploy/sandbox.env")
    payload = await evaluate_cases(
        "eval/cases.yaml",
        report_path=tmp_path / "eval_reports.db",
    )

    assert payload["summary"]["case_count"] == 16
    assert payload["summary"]["passed_count"] == 16
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["overall_case_count"] == 42
    assert payload["summary"]["overall_passed_count"] == 42
    assert payload["summary"]["overall_pass_rate"] == 1.0
    assert payload["summary"]["all_passed"] is True
    assert payload["summary"]["rag_case_count"] == 26
    assert payload["summary"]["rag_passed_count"] == 26
    assert payload["summary"]["p95_latency_ms"] >= 0.0
    assert payload["summary"]["failed_cases"] == []
    assert payload["run"]["started_at"]
    assert payload["run"]["ended_at"]
    assert payload["rag"]["summary"]["case_count"] == 26

    assert set(payload["summary"]["metrics"]) == set(METRIC_NAMES)
    for metric in payload["summary"]["metrics"].values():
        assert metric == {"passed": 16, "total": 16}

    categories = payload["summary"]["categories"]
    assert categories["diagnosis"]["root_cause_hit_rate"] == 1.0
    assert categories["tool"]["tool_hit_rate"] == 1.0
    assert categories["risk"]["forbidden_action_block_rate"] == 1.0
    assert categories["risk"]["approval_recall"] == 1.0
    assert categories["rag"]["recall_at_k"] == 1.0
    assert categories["rag"]["citation_coverage_rate"] == 1.0
    assert categories["rag"]["no_answer_rejection_rate"] == 1.0
    assert categories["rag"]["confusion_case_pass_rate"] == 1.0
    assert categories["rag"]["runbook_no_answer_rejection_hit_rate"] == 1.0
    assert categories["stability"]["tool_failure_case_count"] == 3
    assert categories["stability"]["tool_failure_graceful_degradation_rate"] == 1.0
    assert categories["diagnostic_chain"]["evidence_sufficiency"] == 1.0
    assert categories["diagnostic_chain"]["runtime_vs_incident_boundary"] == 1.0
    assert categories["diagnostic_chain"]["approval_boundary"] == 1.0

    resume_metrics = payload["summary"]["resume_metrics"]
    assert resume_metrics["aiops_case_count"] == 16
    assert resume_metrics["rag_case_count"] == 26
    assert resume_metrics["rag_citation_coverage_rate"] == 1.0
    assert resume_metrics["rag_confusion_case_pass_rate"] == 1.0
    assert resume_metrics["tool_failure_graceful_degradation_rate"] == 1.0
    assert resume_metrics["diagnostic_evidence_sufficiency"] == 1.0
    assert resume_metrics["diagnostic_runtime_vs_incident_boundary"] == 1.0
    assert resume_metrics["diagnostic_approval_boundary"] == 1.0

    for result in payload["cases"]:
        assert result["passed"], result
        assert result["evidence_count"] >= 3
        assert result["report_status"] in {
            "completed",
            "waiting_approval",
            "blocked",
            "degraded",
            "needs_human",
        }
        assert result["confidence"] >= 0.5
        assert result["failed_metrics"] == []
        assert isinstance(result["latency_ms"], float)

    results = {result["id"]: result for result in payload["cases"]}
    assert results["logs_timeout_graceful_degradation"]["failed_tools"] == ["query_logs"]
    assert results["metrics_timeout_redis_degradation"]["failed_tools"] == ["query_metrics"]
    assert results["k8s_permission_denied_incomplete_report"]["failed_tools"] == [
        "query_k8s_status"
    ]
    assert results["k8s_permission_denied_incomplete_report"]["report_status"] == "degraded"
    assert results["runbook_no_answer_rejection"]["runbook_rejected"] is True
    assert results["runbook_no_answer_rejection"]["report_status"] == "needs_human"

    summary_text = render_summary(payload)
    assert "Full eval: 42/42 cases passed" in summary_text
    assert "AIOps eval: 16/16 cases passed" in summary_text
    assert "RAG recall@3=100%" in summary_text
    assert "RAG PASS cpu_high_usage_alert" in summary_text
    assert "restart_service_requires_approval policy=approval_required" in summary_text
    assert "forbidden_delete_pod policy=forbidden" in summary_text
    assert "forbidden_unaudited_sql policy=forbidden" in summary_text

    markdown = render_markdown_summary(payload)
    assert "# AutoOnCall 离线评测摘要" in markdown
    assert "## 简历可摘取指标" in markdown
    assert "完整评测通过率：42/42 (100%)" in markdown
    assert "AIOps 离线 case：16 个" in markdown
    assert "RAG case：26 个" in markdown
    assert "引用覆盖率 100%" in markdown
    assert "混淆 case 通过率 100%" in markdown
    assert "无失败 case" in markdown

    failed_payload = deepcopy(payload)
    rag_failure = {
        "suite": "rag",
        "id": "bad_expected_source",
        "failed_metrics": ["recall_at_k"],
        "failure_reasons": {"recall_at_k": "Top-K 检索结果未命中期望 Runbook 来源。"},
        "expected_sources": ["memory_high_usage.md"],
        "retrieved_sources": ["cpu_high_usage.md"],
    }
    failed_payload["summary"]["failed_cases"] = [rag_failure]
    failed_payload["rag"]["summary"]["failed_cases"] = [rag_failure]
    failed_markdown = render_markdown_summary(failed_payload)
    assert "[rag] bad_expected_source：recall_at_k" in failed_markdown
    assert "期望来源：memory_high_usage.md" in failed_markdown
    assert "实际来源：cpu_high_usage.md" in failed_markdown

    artifacts = write_eval_artifacts(
        payload,
        summary_json_path=tmp_path / "eval_summary.json",
        summary_md_path=tmp_path / "eval_summary.md",
    )
    assert set(artifacts) == {"summary_json", "summary_md"}
    assert (tmp_path / "eval_summary.json").exists()
    assert (tmp_path / "eval_summary.md").exists()
    assert "简历可摘取指标" in (tmp_path / "eval_summary.md").read_text(encoding="utf-8")
