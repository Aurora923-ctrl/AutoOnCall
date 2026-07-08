"""Tests for optional RAGAS quality evaluation wiring."""

from __future__ import annotations

import argparse
import math

import pytest

from scripts.eval.eval_ragas_cases import (
    RagasCaseSample,
    build_case_result,
    build_failed_payload,
    build_quality_contract,
    business_token_overlap,
    context_ids_from_retrieval,
    evaluate_cases,
    extract_business_tokens,
    parse_args,
    reference_context_ids,
    safe_float,
    write_eval_artifacts,
)


def test_ragas_context_ids_use_file_level_granularity() -> None:
    payload = {
        "retrieval_results": [
            {"source_file": "cpu_high_usage.md", "chunk_id": "cpu_high_usage.md#0001"},
            {"source_file": "cpu_high_usage.md", "chunk_id": "cpu_high_usage.md#0002"},
            {"source_file": "slow_response.md", "chunk_id": "slow_response.md#0001"},
        ]
    }
    case = {
        "reference_context_ids": [
            "cpu_high_usage.md#cpu_high_usage.md#0001",
            "cpu_high_usage.md",
        ]
    }

    assert context_ids_from_retrieval(payload) == ["cpu_high_usage.md", "slow_response.md"]
    assert reference_context_ids(case) == ["cpu_high_usage.md"]


def test_ragas_context_ids_fallback_to_source_path_and_chunk_id() -> None:
    payload = {
        "retrieval_results": [
            {"source_path": "E:/kb/redis_postmortem.pdf", "chunk_id": "ignored#0001"},
            {"chunk_id": "payment_wiki.html#0003"},
        ]
    }

    assert context_ids_from_retrieval(payload) == ["redis_postmortem.pdf", "payment_wiki.html"]


def test_business_rubric_token_extraction_supports_chinese_terms() -> None:
    tokens = extract_business_tokens("包含慢 SQL 或连接池等待证据定位")

    assert "sql" in tokens
    assert "连接池" in tokens
    assert business_token_overlap("包含慢 SQL 或连接池等待证据定位", "需要检查慢 SQL 和连接池等待")


def test_ragas_nan_metric_fails_instead_of_passing() -> None:
    sample = RagasCaseSample(
        case={"id": "core", "ragas_tags": ["core_interview"]},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["cpu_high_usage.md"],
        reference_context_ids=["cpu_high_usage.md"],
        answer="answer source_file cpu_high_usage.md chunk_id chunk-1",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "cpu_high_usage.md", "chunk_id": "chunk-1"}],
        retrieval={},
    )

    result = build_case_result(
        sample,
        {
            "faithfulness": math.nan,
            "answer_relevancy": 1.0,
            "id_based_context_precision": 1.0,
            "id_based_context_recall": 1.0,
        },
    )

    assert safe_float(math.nan) == 0.0
    assert result["passed"] is False
    assert "faithfulness" in result["failed_metrics"]


def test_ragas_default_cli_uses_reproducible_smoke_profile() -> None:
    args = parse_args([])

    assert args.metrics_profile == "id-smoke"
    assert args.answer_source == "product-offline"


def test_ragas_quality_contract_explains_id_smoke_watch_metrics() -> None:
    summary = {
        "status": "passed",
        "case_count": 2,
        "quality_case_count": 1,
        "refusal_case_count": 1,
        "pass_rate": 1.0,
        "core_case_count": 2,
        "core_case_pass_rate": 1.0,
        "id_context_precision_avg": 0.5,
        "id_context_recall_avg": 1.0,
        "oncall_actionability_avg": 1.0,
        "citation_grounding_rate": 1.0,
        "incident_boundary_rate": 1.0,
        "refusal_boundary_rate": 1.0,
    }

    contract = build_quality_contract(summary, [], metric_profile="id-smoke")

    assert contract["status"] == "passed"
    hard_gate_keys = {gate["key"] for gate in contract["hard_gates"]}
    watch_keys = {metric["key"] for metric in contract["watch_metrics"]}
    assert "id_context_precision" not in hard_gate_keys
    assert "id_context_precision" in watch_keys
    assert "query_with_retrieval" in contract["interview_talk_track"][1]


def test_ragas_actionability_rejects_generic_answer_even_with_id_hit() -> None:
    sample = RagasCaseSample(
        case={
            "id": "generic_mysql",
            "expected_source": "slow_response.md",
            "business_rubric": [
                "Identify MySQL slow query evidence",
                "Give bounded OnCall action",
            ],
        },
        retrieved_contexts=["mysql context"],
        retrieved_context_ids=["slow_response.md"],
        reference_context_ids=["slow_response.md"],
        answer="mysql source_file slow_response.md chunk_id chunk-1",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "slow_response.md", "chunk_id": "chunk-1"}],
        retrieval={},
    )

    result = build_case_result(
        sample,
        {
            "id_based_context_precision": 1.0,
            "id_based_context_recall": 1.0,
        },
        metric_profile="id-smoke",
    )

    assert result["passed"] is False
    assert "oncall_actionability_score" in result["failed_metrics"]
    assert result["metrics"]["business_domain_hit"] == 1.0
    assert result["metrics"]["business_operation_hit"] == 0.0


def test_ragas_id_smoke_reports_precision_but_gates_on_recall() -> None:
    sample = RagasCaseSample(
        case={
            "id": "noisy_topk",
            "expected_source": "slow_response.md",
            "business_rubric": ["Identify MySQL evidence and approval action"],
        },
        retrieved_contexts=["mysql context", "cpu context"],
        retrieved_context_ids=["slow_response.md", "cpu_high_usage.md"],
        reference_context_ids=["slow_response.md"],
        answer=(
            "mysql evidence runbook incident-window check approval rollback "
            "source_file slow_response.md chunk_id chunk-1"
        ),
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "slow_response.md", "chunk_id": "chunk-1"}],
        retrieval={},
    )

    result = build_case_result(
        sample,
        {
            "id_based_context_precision": 0.5,
            "id_based_context_recall": 1.0,
        },
        metric_profile="id-smoke",
    )

    assert result["passed"] is True
    assert result["metrics"]["id_based_context_precision"] == 0.5


@pytest.mark.asyncio
async def test_ragas_refusal_only_fixture_skips_metric_runner(monkeypatch, tmp_path) -> None:
    cases_path = tmp_path / "ragas_refusal_cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: reject_resume_question
    query: 简历怎么包装？
    should_reject: true
    ragas_tags:
      - refusal_boundary
""",
        encoding="utf-8",
    )

    async def fake_query_with_retrieval(self, question, session_id, metadata_filter=None):
        return {
            "answer": "未找到可信知识来源，请补充知识库文档。",
            "citations": [],
            "retrieval": {
                "status": "no_answer",
                "retrieval_results": [],
                "answer_policy": "refuse_without_trusted_source",
            },
            "no_answer": True,
            "answer_policy": "refuse_without_trusted_source",
        }

    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.RagAgentService.query_with_retrieval",
        fake_query_with_retrieval,
    )

    def forbidden_runner(samples, runner_context):
        raise AssertionError("refusal-only runs should not call RAGAS metrics")

    payload = await evaluate_cases(
        cases_path,
        docs_dir="aiops-docs",
        answer_source="reference-fixture",
        metrics_runner=forbidden_runner,
    )

    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["refusal_case_count"] == 1
    assert payload["summary"]["refusal_boundary_rate"] == 1.0
    assert payload["case_scores"][0]["answer_policy"] == "refuse_without_trusted_source"


@pytest.mark.asyncio
async def test_ragas_id_smoke_positive_case_writes_artifacts(tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "payment_wiki.md").write_text(
        """
# Payment Runbook

MySQL slow query evidence includes EXPLAIN, active_connections, pool_waiting,
incident-window metrics, rollback approval, and source_file chunk_id citations.
""",
        encoding="utf-8",
    )
    cases_path = tmp_path / "ragas_cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: payment_mysql_smoke
    query: Payment MySQL pool_waiting active_connections approval
    expected_source: payment_wiki.md
    reference_answer: >
      Payment MySQL slow query should be checked with EXPLAIN, pool_waiting,
      active_connections, incident-window metrics, and approval before rollback.
    reference_context_ids:
      - payment_wiki.md
    ragas_tags:
      - core_interview
    business_rubric:
      - Identify MySQL slow query evidence
      - Mention pool_waiting or active_connections
      - Keep rollback inside approval boundary
""",
        encoding="utf-8",
    )

    payload = await evaluate_cases(
        cases_path,
        docs_dir=docs_dir,
        answer_source="reference-fixture",
        top_k=1,
        min_score=0.1,
    )
    written = write_eval_artifacts(
        payload,
        summary_json_path=tmp_path / "ragas_summary.json",
        summary_md_path=tmp_path / "ragas_summary.md",
    )

    assert payload["run"]["metric_profile"] == "id-smoke"
    assert payload["run"]["answer_source"] == "reference-fixture"
    assert payload["quality_contract"]["status"] == "passed"
    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["core_case_pass_rate"] == 1.0
    assert payload["case_scores"][0]["metrics"]["id_based_context_recall"] == 1.0
    assert "summary_json" in written
    assert (tmp_path / "ragas_summary.json").exists()
    assert "Metric profile: `id-smoke`" in (tmp_path / "ragas_summary.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_ragas_product_offline_uses_query_with_retrieval_for_all_cases(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "redis_postmortem.md").write_text(
        "Redis maxclients evidence includes connected_clients, incident-window, "
        "approval, source_file and chunk_id.",
        encoding="utf-8",
    )
    cases_path = tmp_path / "ragas_cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis_core
    query: Redis maxclients connected_clients approval
    expected_source: redis_postmortem.md
    reference_answer: >
      Redis maxclients should check connected_clients, incident-window evidence,
      and approval before limit or scale actions.
    reference_context_ids:
      - redis_postmortem.md
    ragas_tags:
      - core_interview
    business_rubric:
      - Mention Redis maxclients evidence
      - Keep action inside approval boundary
  - id: reject_resume_question
    query: 简历怎么包装？
    should_reject: true
    ragas_tags:
      - refusal_boundary
""",
        encoding="utf-8",
    )
    calls: list[str] = []
    original = __import__(
        "scripts.eval.eval_ragas_cases",
        fromlist=["RagAgentService"],
    ).RagAgentService.query_with_retrieval

    async def spy_query_with_retrieval(self, question, session_id, metadata_filter=None):
        calls.append(session_id)
        return await original(self, question, session_id, metadata_filter)

    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.RagAgentService.query_with_retrieval",
        spy_query_with_retrieval,
    )

    payload = await evaluate_cases(
        cases_path,
        docs_dir=docs_dir,
        top_k=1,
        min_score=0.1,
    )

    assert payload["run"]["answer_source"] == "product-offline"
    assert len(calls) == 2
    assert payload["summary"]["status"] == "passed"
    assert payload["case_scores"][1]["answer_policy"] == "refuse_without_trusted_source"


def test_ragas_setup_failure_payload_is_structured() -> None:
    args = argparse.Namespace(
        cases="eval/rag_cases.yaml",
        docs_dir="aiops-docs",
        mode="offline",
        answer_source="context-fixture",
        metrics_profile="id-smoke",
        top_k=3,
        min_score=2.0,
    )

    payload = build_failed_payload(args, RuntimeError("missing judge key"))

    assert payload["summary"]["status"] == "failed"
    assert payload["summary"]["failed_cases"][0]["id"] == "ragas_setup"
    assert payload["run"]["ragas_version"]
    assert payload["thresholds"]["core_case_pass_rate"] == 1.0
