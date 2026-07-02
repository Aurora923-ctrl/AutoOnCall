"""Tests for safe-change offline evaluation cases."""

import pytest

from scripts.eval.eval_change_cases import (
    evaluate_cases,
    load_cases,
    render_markdown_summary,
    render_summary,
)


def test_change_eval_cases_cover_safe_change_boundaries() -> None:
    cases = load_cases("eval/change_cases.yaml")
    case_ids = {case["id"] for case in cases}

    assert "redis_maxclients_safe_change_success" in case_ids
    assert "redis_maxclients_precheck_stale_evidence" in case_ids
    assert "redis_maxclients_dry_run_failed" in case_ids
    assert "redis_maxclients_observation_failed_rollback_recommended" in case_ids
    assert "forbidden_sql_never_enters_change_execution" in case_ids
    assert "approval_required_before_change_execution" in case_ids
    assert "rejected_approval_before_change_execution" in case_ids
    assert "staging_sandbox_validated" in case_ids
    assert "prod_sandbox_without_flag_escalates" in case_ids
    assert len(cases) == 9

    safe_change_cases = [case for case in cases if case.get("scenario") == "safe_change"]
    forbidden_cases = [case for case in cases if case.get("scenario") == "forbidden_policy"]
    sandbox_cases = [case for case in cases if case.get("mode") == "sandbox"]
    rejected_or_pending_cases = [
        case for case in cases if case.get("approval_status") in {"pending", "rejected"}
    ]

    assert len(safe_change_cases) >= 8
    assert len(forbidden_cases) >= 1
    assert len(sandbox_cases) >= 2
    assert len(rejected_or_pending_cases) >= 2


@pytest.mark.asyncio
async def test_change_eval_cases_all_pass_offline() -> None:
    payload = await evaluate_cases("eval/change_cases.yaml")

    assert payload["summary"]["case_count"] == 9
    assert payload["summary"]["passed_count"] == 9
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["all_passed"] is True
    assert payload["summary"]["failed_cases"] == []

    resume_metrics = payload["summary"]["resume_metrics"]
    assert resume_metrics["change_plan_completeness"] == 1.0
    assert resume_metrics["precheck_recall"] == 1.0
    assert resume_metrics["dry_run_before_execute_rate"] == 1.0
    assert resume_metrics["approval_before_execute_rate"] == 1.0
    assert resume_metrics["rollback_recommendation_rate"] == 1.0
    assert resume_metrics["forbidden_change_block_rate"] == 1.0

    result_by_id = {result["id"]: result for result in payload["cases"]}
    assert result_by_id["rejected_approval_before_change_execution"]["actual_status"] == (
        "rejected_before_execution"
    )
    assert result_by_id["staging_sandbox_validated"]["actual_status"] == "sandbox_validated"
    assert result_by_id["prod_sandbox_without_flag_escalates"]["actual_status"] == "escalated"

    summary_text = render_summary(payload)
    assert "Safe-change eval: 9/9 cases passed" in summary_text
    assert "forbidden_block=100%" in summary_text

    markdown = render_markdown_summary(payload)
    assert "安全变更评测通过率：9/9 (100%)" in markdown
    assert "`staging_sandbox_validated`" in markdown
