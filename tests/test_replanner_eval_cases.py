"""Tests for offline Replanner LLM decision evaluation cases."""

import pytest

from scripts.eval.eval_replanner_cases import (
    evaluate_cases,
    load_cases,
    render_markdown_summary,
    render_summary,
)


def test_replanner_eval_cases_cover_llm_guardrails() -> None:
    cases = load_cases("eval/replanner_cases.yaml")
    case_ids = {case["id"] for case in cases}

    assert "llm_adds_registered_read_only_steps" in case_ids
    assert "llm_adds_read_only_trace_step" in case_ids
    assert "llm_generate_report_blocked_when_evidence_insufficient" in case_ids
    assert "llm_unsafe_tool_falls_back_to_evidence_analyzer" in case_ids
    assert "failed_tool_retry_skips_llm_decision" in case_ids
    assert len(cases) == 5

    for case in cases:
        assert case.get("llm_decision")
        assert case.get("expected_decision")
        assert case.get("expected_decision_source")


@pytest.mark.asyncio
async def test_replanner_eval_cases_all_pass_offline() -> None:
    payload = await evaluate_cases("eval/replanner_cases.yaml")

    assert payload["summary"]["case_count"] == 5
    assert payload["summary"]["passed_count"] == 5
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["all_passed"] is True
    assert payload["summary"]["failed_cases"] == []

    resume_metrics = payload["summary"]["resume_metrics"]
    assert resume_metrics["decision_source_hit_rate"] == 1.0
    assert resume_metrics["guardrail_hit_rate"] == 1.0
    assert resume_metrics["forbidden_tools_avoided_rate"] == 1.0
    assert resume_metrics["llm_call_policy_hit_rate"] == 1.0
    assert resume_metrics["trace_decision_recorded_rate"] == 1.0

    result_by_id = {result["id"]: result for result in payload["cases"]}
    assert result_by_id["llm_adds_registered_read_only_steps"]["actual_decision_source"] == (
        "llm_structured"
    )
    assert result_by_id["llm_adds_registered_read_only_steps"]["actual_plan_tools"] == [
        "query_metrics",
        "query_redis_status",
    ]
    assert result_by_id["llm_adds_read_only_trace_step"]["actual_plan_tools"] == [
        "query_metrics",
        "query_redis_status"
    ]
    assert result_by_id["llm_adds_read_only_trace_step"]["actual_decision_source"] == (
        "evidence_analyzer_fallback"
    )
    assert result_by_id[
        "llm_generate_report_blocked_when_evidence_insufficient"
    ]["actual_decision"] == "add_steps"
    assert result_by_id[
        "llm_unsafe_tool_falls_back_to_evidence_analyzer"
    ]["actual_decision_source"] == "evidence_analyzer_fallback"
    assert result_by_id["failed_tool_retry_skips_llm_decision"]["llm_call_count"] == 0
    assert result_by_id["failed_tool_retry_skips_llm_decision"]["first_step_id"] == "s3-retry"

    summary_text = render_summary(payload)
    assert "Replanner eval: 5/5 cases passed" in summary_text
    assert "guardrail=100%" in summary_text
    assert "llm_structured=100%" in summary_text

    markdown = render_markdown_summary(payload)
    assert "Replanner 评测通过率：5/5 (100%)" in markdown
    assert "LLM structured 正向路径命中率：100%" in markdown
    assert "`llm_adds_read_only_trace_step`" in markdown
