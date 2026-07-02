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
    assert "cpu_high_but_root_cause_slow_query" in case_ids
    assert "service_503_but_dependency_timeout" in case_ids
    assert "reject_stock_investment" in case_ids
    assert "reject_resume_question" in case_ids
    assert len(cases) >= 20

    reject_cases = [case for case in cases if case.get("should_reject")]
    assert len(reject_cases) >= 5
    confusion_cases = [case for case in cases if case.get("case_type") == "confusion"]
    assert len(confusion_cases) >= 4
    for case in cases:
        if case.get("should_reject"):
            continue
        assert case.get("expected_source")
        assert case.get("expected_keywords")


def test_rag_eval_cases_all_pass_offline() -> None:
    payload = evaluate_cases("eval/rag_cases.yaml", docs_dir="aiops-docs")

    assert payload["summary"]["case_count"] == 22
    assert payload["summary"]["passed_count"] == 22
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["recall_at_k"] == 1.0
    assert payload["summary"]["citation_coverage_rate"] == 1.0
    assert payload["summary"]["no_answer_rejection_rate"] == 1.0
    assert payload["summary"]["confusion_case_pass_rate"] == 1.0
    assert payload["summary"]["reject_case_count"] >= 5
    assert payload["summary"]["confusion_case_count"] >= 4
    assert payload["summary"]["mrr"] >= 0.9

    summary_text = render_summary(payload)
    assert "RAG eval: 22/22 cases passed" in summary_text
    assert "recall@3=100%" in summary_text
    assert "cite=100%" in summary_text
    assert "confusion=100%" in summary_text
    assert "reject=100%" in summary_text

    for result in payload["cases"]:
        assert result["failed_metrics"] == []
        assert result["failure_reasons"] == {}
        if not result["should_reject"]:
            assert result["citation_hit"] is True


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


def test_rag_eval_case_failure_identifies_missing_citation() -> None:
    result = evaluate_case(
        {
            "id": "missing_citation",
            "query": "Redis timeout",
            "expected_source": "redis.md",
            "expected_keywords": [],
        },
        [
            {
                "source_file": "",
                "chunk_id": "",
                "content": "Redis timeout runbook",
                "heading_path": "",
                "offline_terms": {"redis", "timeout"},
            }
        ],
        top_k=1,
        min_score=0.1,
    )

    assert result["passed"] is False
    assert "citation_coverage" in result["failed_metrics"]
    assert "缺少 source_file + chunk_id" in result["failure_reasons"]["citation_coverage"]
