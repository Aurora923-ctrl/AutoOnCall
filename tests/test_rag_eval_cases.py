"""Tests for offline RAG retrieval evaluation cases."""

from scripts.eval_rag_cases import (
    build_offline_index,
    evaluate_case,
    evaluate_cases,
    load_cases,
    render_summary,
)


def test_rag_cases_cover_core_runbook_types_and_rejection() -> None:
    cases = load_cases("eval/rag_cases.yaml")
    case_ids = {case["id"] for case in cases}

    assert "cpu_high_usage_alert" in case_ids
    assert "memory_oom" in case_ids
    assert "disk_no_space" in case_ids
    assert "service_503_unavailable" in case_ids
    assert "slow_response_sql" in case_ids
    assert "reject_resume_question" in case_ids
    assert len(cases) >= 15

    reject_cases = [case for case in cases if case.get("should_reject")]
    assert len(reject_cases) >= 3
    for case in cases:
        if case.get("should_reject"):
            continue
        assert case.get("expected_source")
        assert case.get("expected_keywords")


def test_rag_eval_cases_all_pass_offline() -> None:
    payload = evaluate_cases("eval/rag_cases.yaml", docs_dir="aiops-docs")

    assert payload["summary"]["case_count"] == 17
    assert payload["summary"]["passed_count"] == 17
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["recall_at_k"] == 1.0
    assert payload["summary"]["no_answer_rejection_rate"] == 1.0
    assert payload["summary"]["mrr"] >= 0.9

    summary_text = render_summary(payload)
    assert "RAG eval: 17/17 cases passed" in summary_text
    assert "recall@3=100%" in summary_text
    assert "reject=100%" in summary_text

    for result in payload["cases"]:
        assert result["failed_metrics"] == []
        assert result["failure_reasons"] == {}


def test_rag_eval_case_failure_identifies_failed_metric() -> None:
    index = build_offline_index("aiops-docs")
    result = evaluate_case(
        {
            "id": "bad_expected_source",
            "query": "billing-service CPU 使用率持续 95%",
            "expected_source": "missing_runbook.md",
            "expected_keywords": ["CPU"],
        },
        index,
        top_k=3,
        min_score=2.0,
    )

    assert result["passed"] is False
    assert result["failed_metrics"] == ["recall_at_k"]
    assert "Top-K 检索结果未命中" in result["failure_reasons"]["recall_at_k"]
    assert result["expected_sources"] == ["missing_runbook.md"]
    assert "cpu_high_usage.md" in result["retrieved_sources"]
