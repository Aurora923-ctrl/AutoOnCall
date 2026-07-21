"""Tests for optional RAGAS quality evaluation wiring."""

from __future__ import annotations

import argparse
import json
import math

import pytest

from scripts.eval.eval_ragas_cases import (
    RagasCaseSample,
    answer_for_judge,
    build_case_result,
    build_failed_payload,
    build_human_review_template,
    build_quality_contract,
    business_domain_hit,
    business_evidence_hit,
    business_operation_hit,
    business_requirement_hit,
    business_token_overlap,
    citation_quality_scores,
    compare_human_reviews,
    context_ids_from_retrieval,
    contexts_for_citations,
    evaluate_cases,
    extract_business_tokens,
    judge_execution_status,
    load_cases,
    load_human_reviews,
    load_ragas_metric_classes,
    parse_args,
    ragas_execution_markdown_lines,
    reference_context_ids,
    review_item_set_sha256,
    run_ragas_id_smoke_metrics,
    safe_float,
    validate_ragas_input,
    write_eval_artifacts,
)


def test_installed_ragas_metric_classes_resolve_without_private_exports() -> None:
    metrics = load_ragas_metric_classes()

    assert metrics["Faithfulness"].__name__ == "Faithfulness"
    assert metrics["ResponseRelevancy"].__name__ in {"ResponseRelevancy", "AnswerRelevancy"}
    assert not any(metric.__name__.startswith("_") for metric in metrics.values())


def test_full_profile_metric_average_requires_complete_case_coverage() -> None:
    from scripts.eval.eval_ragas_cases import average_optional_metric

    results = [
        {"id": "case-a", "metrics": {"faithfulness": 1.0}},
        {"id": "case-b", "metrics": {"faithfulness": None}},
    ]

    assert average_optional_metric(results, "faithfulness") is None


def test_repeat_metric_stability_requires_every_repeat() -> None:
    from scripts.eval.eval_ragas_cases import optional_numeric_stability

    assert optional_numeric_stability([1.0, None, 1.0]) is None


def test_ragas_report_exposes_metric_engine_and_exact_coverage() -> None:
    lines = ragas_execution_markdown_lines(
        {
            "id_metric_execution": {
                "engine": "deterministic_fallback",
                "status": "fallback",
                "reason": "ImportError: incompatible RAGAS",
            }
        },
        {
            "metric_coverage": {
                "faithfulness": {
                    "available_count": 1,
                    "expected_count": 2,
                    "missing_case_ids": ["case-b"],
                }
            }
        },
    )

    text = "\n".join(lines)
    assert "deterministic_fallback/fallback" in text
    assert "ImportError: incompatible RAGAS" in text
    assert "| `faithfulness` | 0 | 2 | 1 | case-b |" in text


def test_ragas_context_ids_use_chunk_granularity_when_relevance_labels_are_chunks() -> None:
    payload = {
        "retrieval_results": [
            {"source_file": "cpu_high_usage.md", "chunk_id": "cpu_high_usage.md#0001"},
            {"source_file": "cpu_high_usage.md", "chunk_id": "cpu_high_usage.md#0002"},
        ]
    }
    case = {
        "relevant_chunks": [
            {"chunk_id": "cpu_high_usage.md#0002", "relevance": 3},
        ]
    }
    references = reference_context_ids(case)

    assert references == ["cpu_high_usage.md#0002"]
    assert context_ids_from_retrieval(payload, reference_ids=references) == [
        "cpu_high_usage.md#0001",
        "cpu_high_usage.md#0002",
    ]


def test_answer_for_judge_removes_inline_and_footer_citation_metadata() -> None:
    answer = (
        "建议检查 EndpointSlice [services.md | services.md#0013]。\n\n"
        "引用来源：\n"
        "- source_file: services.md; chunk_id: services.md#0013"
    )

    assert answer_for_judge(answer) == "建议检查 EndpointSlice 。"


def test_business_metrics_recognize_normal_chinese_oncall_language() -> None:
    answer = "建议检查系统日志和内存指标，确认影响后进入人工审批。"

    assert business_evidence_hit(answer)
    assert business_operation_hit(answer)
    sample = RagasCaseSample(
        case={"query": "内存持续升高怎么办", "expected_source": "memory_high_usage.md"},
        retrieved_contexts=["内存 Runbook"],
        retrieved_context_ids=["memory_high_usage.md"],
        reference_context_ids=["memory_high_usage.md"],
        answer=answer,
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "memory_high_usage.md", "chunk_id": "memory.md#1"}],
        retrieval={},
    )
    assert business_domain_hit(sample)


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


def test_contexts_for_citations_aligns_text_by_chunk_id_and_citation_order() -> None:
    retrieval = {
        "retrieval_results": [
            {
                "source_file": "wiki.html",
                "chunk_id": "wiki.html#0001",
                "content": "Background context that the answer did not cite.",
            },
            {
                "source_file": "tickets.csv",
                "chunk_id": "tickets.csv#0002",
                "content": "INC-REDIS-009 ticket row.",
            },
            {
                "source_file": "postmortem.pdf",
                "chunk_id": "postmortem.pdf#0001",
                "content": "Redis maxclients incident evidence.",
            },
        ]
    }
    citations = [
        {"source_file": "postmortem.pdf", "chunk_id": "postmortem.pdf#0001"},
        {"source_file": "tickets.csv", "chunk_id": "tickets.csv#0002"},
    ]

    assert contexts_for_citations(retrieval, citations) == [
        "Redis maxclients incident evidence.",
        "INC-REDIS-009 ticket row.",
    ]


def test_contexts_for_citations_falls_back_when_citation_is_not_in_retrieval() -> None:
    retrieval = {
        "retrieval_results": [
            {
                "source_file": "runbook.md",
                "chunk_id": "runbook.md#0001",
                "content": "Trusted runbook context.",
            }
        ]
    }

    assert contexts_for_citations(
        retrieval,
        [{"source_file": "missing.md", "chunk_id": "missing.md#0001"}],
    ) == ["Trusted runbook context."]


def test_ragas_context_ids_fallback_to_source_path_and_chunk_id() -> None:
    payload = {
        "retrieval_results": [
            {"source_path": "E:/kb/redis_postmortem.pdf", "chunk_id": "ignored#0001"},
            {"chunk_id": "payment_wiki.html#0003"},
        ]
    }

    assert context_ids_from_retrieval(payload) == ["redis_postmortem.pdf", "payment_wiki.html"]


def test_ragas_input_gate_reports_missing_runtime_context() -> None:
    assert validate_ragas_input(
        {"required_sources": ["redis.md"]},
        {"status": "success", "retrieval_results": []},
        retrieved_ids=[],
        contexts=[],
    ) == ["retrieved_contexts_missing", "retrieved_context_ids_missing"]


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
    assert result["metrics"]["faithfulness"] is None
    assert "faithfulness_unavailable" in result["failed_metrics"]


def test_ragas_id_metric_nan_is_reported_unavailable(monkeypatch) -> None:
    sample = RagasCaseSample(
        case={"id": "nan-id"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md"],
        reference_context_ids=["redis.md"],
        answer="answer",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[],
        retrieval={},
    )
    runner_context = {}

    class Result:
        def to_pandas(self):
            class Frame:
                @staticmethod
                def to_dict(*, orient):
                    assert orient == "records"
                    return [
                        {
                            "id_based_context_precision": math.nan,
                            "id_based_context_recall": 1.0,
                        }
                    ]

            return Frame()

    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.load_ragas_metric_classes",
        lambda: {
            "AspectCritic": object,
            "Faithfulness": object,
            "IDBasedContextPrecision": type("Precision", (), {}),
            "IDBasedContextRecall": type("Recall", (), {}),
            "ResponseRelevancy": object,
        },
    )
    monkeypatch.setattr("ragas.evaluate", lambda *args, **kwargs: Result())

    scores = run_ragas_id_smoke_metrics([sample], runner_context)

    assert scores["nan-id"]["id_based_context_precision"] is None
    assert runner_context["id_metric_execution"]["status"] == "failed"


def test_full_profile_marks_missing_judge_metric_as_unavailable() -> None:
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
            "faithfulness": 1.0,
            "answer_relevancy": None,
            "judge_oncall_actionability": 1.0,
            "answer_completeness": 1.0,
            "id_based_context_precision": 1.0,
            "id_based_context_recall": 1.0,
        },
        metric_profile="full",
    )

    assert result["metrics"]["answer_relevancy"] is None
    assert result["judge_metrics_status"] == "failed"
    assert "answer_relevancy_unavailable" in result["failed_metrics"]


def test_ragas_default_cli_uses_reproducible_smoke_profile() -> None:
    args = parse_args([])

    assert args.metrics_profile == "id-smoke"
    assert args.answer_source == "product-offline"
    assert args.repeat_count == 1
    assert args.failed_cases_json is None


def test_stage3_core_dataset_has_reviewable_enterprise_shape() -> None:
    cases = load_cases("eval/ragas_stage3_core_cases.yaml")

    assert 10 <= len(cases) <= 15
    assert sum(bool(case.get("should_reject")) for case in cases) >= 2
    assert all("core_interview" in case.get("ragas_tags", []) for case in cases)
    assert all(case.get("business_rubric") and case.get("reference_answer") for case in cases)
    assert any(len(reference_context_ids(case)) > 1 for case in cases)


def test_business_requirement_hit_understands_oncall_boundary_paraphrases() -> None:
    answer = (
        "Check incident-window metric and log evidence first, then keep approval, "
        "dry-run, and rollback boundaries before remediation."
    )

    assert business_requirement_hit("区分诊断证据和处置动作", answer)
    assert business_requirement_hit("处置动作保留审批或回滚边界", answer)
    assert business_requirement_hit(
        "不把相关性直接当作根因",
        "应结合慢查询、连接池等待和当前影响判断。",
    )


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


def test_ragas_id_smoke_missing_recall_fails_instead_of_being_skipped() -> None:
    sample = RagasCaseSample(
        case={
            "id": "missing-id",
            "expected_source": "redis.md",
            "business_rubric": ["Redis evidence approval"],
        },
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md"],
        reference_context_ids=["redis.md"],
        answer="Redis evidence approval source_file redis.md chunk_id redis.md#1",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "redis.md", "chunk_id": "redis.md#1"}],
        retrieval={},
    )

    result = build_case_result(
        sample,
        {"id_based_context_precision": 1.0},
        metric_profile="id-smoke",
    )

    assert result["passed"] is False
    assert "id_based_context_recall_unavailable" in result["failed_metrics"]


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
        docs_dir="docs/knowledge-base",
        answer_source="reference-fixture",
        metrics_runner=forbidden_runner,
    )

    assert payload["summary"]["status"] == "not_run"
    assert payload["summary"]["deterministic_status"] == "passed"
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
    required_sources:
      - payment_wiki.md
    relevant_chunks:
      - payment_wiki.md
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
    assert payload["run"]["answer_generation_executed"] is False
    assert payload["quality_contract"]["status"] == "not_run"
    assert payload["summary"]["status"] == "not_run"
    assert payload["summary"]["deterministic_status"] == "passed"
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
    required_sources:
      - redis_postmortem.md
    relevant_chunks:
      - redis_postmortem.md
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
    assert payload["run"]["answer_generation_mode"] == "reference_fixture_via_product_contract"
    assert payload["run"]["answer_generation_executed"] is False
    assert payload["run"]["retrieval_evidence_mode"] == "fixed_offline_retrieval"
    assert len(calls) == 2
    assert payload["summary"]["status"] == "not_run"
    assert payload["summary"]["deterministic_status"] == "passed"
    assert payload["case_scores"][1]["answer_policy"] == "refuse_without_trusted_source"


@pytest.mark.asyncio
async def test_context_fixture_uses_real_grounded_generation_with_fixed_retrieval(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "redis.md").write_text(
        "Redis maxclients requires checking connected_clients before approved changes.",
        encoding="utf-8",
    )
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis_generated
    query: Redis maxclients connected_clients approval
    expected_source: redis.md
    reference_context_ids:
      - redis.md
    reference_answer: Check Redis connected_clients before approved changes.
    business_rubric:
      - Redis maxclients evidence
      - approval boundary
""",
        encoding="utf-8",
    )
    generated_prompts: list[str] = []

    async def fake_query_grounded(self, grounded_question, session_id, *, history_question=None):
        generated_prompts.append(grounded_question)
        return (
            "Generated from the supplied context: check connected_clients before approval. "
            "[redis.md | redis.md#0001]"
        )

    async def fake_query_grounded_observed(
        self,
        grounded_question,
        session_id,
        *,
        history_question=None,
    ):
        return (
            await fake_query_grounded(
                self,
                grounded_question,
                session_id,
                history_question=history_question,
            ),
            {
                "llm_generation_ms": 1.0,
                "llm_ttft_ms": "not_observed",
                "token_usage": {"status": "not_observed"},
                "model": "test-real-model",
            },
        )

    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.RagAgentService.query_grounded",
        fake_query_grounded,
    )
    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.RagAgentService.query_grounded_observed",
        fake_query_grounded_observed,
    )

    payload = await evaluate_cases(
        cases_path,
        docs_dir=docs_dir,
        answer_source="context-fixture",
        top_k=1,
        min_score=0.1,
        human_review_path=None,
    )

    assert generated_prompts
    assert payload["run"]["answer_generation_mode"] == "real_grounded_llm"
    assert payload["run"]["retrieval_evidence_mode"] == "fixed_offline_retrieval"
    assert "Generated from the supplied context" in payload["case_scores"][0]["answer"]


@pytest.mark.asyncio
async def test_runtime_ragas_scores_the_same_retrieval_returned_by_product(
    monkeypatch,
    tmp_path,
) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: runtime-consistency
    query: Redis maxclients?
    expected_source: redis.md
    reference_context_ids: [redis.md]
    business_rubric: [Redis evidence]
""",
        encoding="utf-8",
    )

    def forbidden_retrieval(*args, **kwargs):
        raise AssertionError("runtime RAGAS must not issue a second retrieval")

    async def fake_product_call(self, question, session_id, metadata_filter=None):
        return {
            "answer": "Redis evidence [redis.md | redis.md#0001]",
            "answer_policy": "answer_with_citations",
            "no_answer": False,
            "citations": [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
            "retrieval": {
                "status": "success",
                "retrieval_results": [
                    {
                        "source_file": "redis.md",
                        "chunk_id": "redis.md#0001",
                        "content_preview": "Redis maxclients evidence",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.retrieve_structured_knowledge",
        forbidden_retrieval,
    )
    monkeypatch.setattr(
        "scripts.eval.eval_ragas_cases.RagAgentService.query_with_retrieval",
        fake_product_call,
    )

    payload = await evaluate_cases(
        cases_path,
        docs_dir=tmp_path,
        mode="runtime",
        answer_source="runtime",
        metric_profile="id-smoke",
        human_review_path=None,
    )

    result = payload["case_scores"][0]
    assert result["retrieved_context_ids"] == ["redis.md"]
    assert result["retrieved_contexts"] == ["Redis maxclients evidence"]


def test_ragas_setup_failure_payload_is_structured() -> None:
    args = argparse.Namespace(
        cases="eval/rag_cases.yaml",
        docs_dir="docs/knowledge-base",
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


def test_citation_quality_separates_existence_support_and_correctness() -> None:
    sample = RagasCaseSample(
        case={"id": "citation", "expected_source": "expected.md"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["expected.md", "extra.md"],
        reference_context_ids=["expected.md"],
        answer="source_file extra.md chunk_id extra.md#0001",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "extra.md", "chunk_id": "extra.md#0001"}],
        retrieval={},
    )

    metrics = citation_quality_scores(sample)

    assert metrics["citation_existence_hit"] == 1.0
    assert metrics["citation_support_score"] == 1.0
    assert metrics["citation_correctness_score"] == 0.0

    result = build_case_result(
        sample,
        {
            "id_based_context_precision": 0.5,
            "id_based_context_recall": 1.0,
        },
        metric_profile="id-smoke",
    )
    assert "citation_correctness_score" not in result["failed_metrics"]


def test_citation_quality_resolves_numbered_citation_to_exact_chunk() -> None:
    sample = RagasCaseSample(
        case={"id": "numbered", "expected_source": "redis.md"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md#0002"],
        reference_context_ids=["redis.md#0002"],
        answer="检查连接池。[证据 2]",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[
            {
                "citation_index": 1,
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
            },
            {
                "citation_index": 2,
                "source_file": "redis.md",
                "chunk_id": "redis.md#0002",
            },
        ],
        retrieval={},
    )

    metrics = citation_quality_scores(sample)

    assert metrics == {
        "citation_grounding_hit": 1.0,
        "citation_existence_hit": 1.0,
        "citation_support_score": 1.0,
        "citation_correctness_score": 1.0,
    }


def test_citation_quality_fails_closed_for_unknown_numbered_citation() -> None:
    sample = RagasCaseSample(
        case={"id": "unknown-number", "expected_source": "redis.md"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md#0001"],
        reference_context_ids=["redis.md#0001"],
        answer="检查连接池。[证据 99]",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[
            {
                "citation_index": 1,
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
            }
        ],
        retrieval={},
    )

    metrics = citation_quality_scores(sample)

    assert metrics["citation_existence_hit"] == 0.0
    assert metrics["citation_support_score"] == 0.0
    assert metrics["citation_correctness_score"] == 0.0


def test_citation_quality_distinguishes_neighboring_chunks_in_same_source() -> None:
    sample = RagasCaseSample(
        case={"id": "wrong-chunk", "expected_source": "redis.md"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md#0001", "redis.md#0002"],
        reference_context_ids=["redis.md#0001"],
        answer="检查连接池。[证据 2]",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[
            {
                "citation_index": 1,
                "source_file": "redis.md",
                "chunk_id": "redis.md#0001",
            },
            {
                "citation_index": 2,
                "source_file": "redis.md",
                "chunk_id": "redis.md#0002",
            },
        ],
        retrieval={},
    )

    metrics = citation_quality_scores(sample)

    assert metrics["citation_existence_hit"] == 1.0
    assert metrics["citation_support_score"] == 1.0
    assert metrics["citation_correctness_score"] == 0.0


def test_citation_quality_keeps_legacy_source_chunk_format_compatible() -> None:
    sample = RagasCaseSample(
        case={"id": "legacy", "expected_source": "redis.md"},
        retrieved_contexts=["ctx"],
        retrieved_context_ids=["redis.md#0001"],
        reference_context_ids=["redis.md#0001"],
        answer="检查连接池。[redis.md | redis.md#0001]",
        answer_policy="answer_with_citations",
        no_answer=False,
        citations=[{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
        retrieval={},
    )

    metrics = citation_quality_scores(sample)

    assert metrics["citation_existence_hit"] == 1.0
    assert metrics["citation_support_score"] == 1.0
    assert metrics["citation_correctness_score"] == 1.0


def test_human_review_comparison_reports_agreement() -> None:
    comparison = compare_human_reviews(
        [{"id": "case-a", "passed": True}, {"id": "case-b", "passed": False}],
        {
            "case-a": {"case_id": "case-a", "reviewer": "sre", "decision": "pass"},
            "case-b": {"case_id": "case-b", "reviewer": "sre", "decision": "pass"},
        },
    )

    assert comparison["status"] == "available"
    assert comparison["reviewed_case_count"] == 2
    assert comparison["agreement_rate"] == 0.5


@pytest.mark.asyncio
async def test_ragas_repeat_count_retains_raw_runs_and_stability(tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "redis.md").write_text(
        "Redis maxclients connected_clients incident-window evidence approval.",
        encoding="utf-8",
    )
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis_repeat
    query: Redis maxclients connected_clients approval
    expected_source: redis.md
    required_sources:
      - redis.md
    relevant_chunks:
      - redis.md
    reference_context_ids:
      - redis.md
    reference_answer: Redis maxclients needs connected_clients evidence and approval.
    business_rubric:
      - Redis maxclients evidence
      - approval action
""",
        encoding="utf-8",
    )

    payload = await evaluate_cases(
        cases_path,
        docs_dir=docs_dir,
        answer_source="reference-fixture",
        top_k=1,
        min_score=0.1,
        repeat_count=3,
        human_review_path=None,
    )

    assert payload["run"]["repeat_count"] == 3
    assert len(payload["case_scores"][0]["repeat_results"]) == 3
    assert payload["case_scores"][0]["stability"]["all_pass"] is True
    assert payload["summary"]["stability"]["all_cases_all_pass"] is True
    assert payload["case_scores"][0]["stability"]["metrics"]["id_based_context_recall"] == {
        "mean": 1.0,
        "std": 0.0,
        "worst": 1.0,
    }


@pytest.mark.asyncio
async def test_full_profile_without_judge_key_is_not_run(monkeypatch, tmp_path) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: one
    query: Redis incident-window evidence approval
    expected_source: redis.md
    required_sources:
      - redis.md
    relevant_chunks:
      - redis.md
    reference_context_ids:
      - redis.md
    reference_answer: Check Redis incident-window evidence and require approval.
    business_rubric:
      - Redis incident evidence
      - approval boundary
""",
        encoding="utf-8",
    )
    (tmp_path / "redis.md").write_text(
        "Redis incident-window evidence approval source_file chunk_id.",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.eval.eval_ragas_cases.config.dashscope_api_key", "")

    payload = await evaluate_cases(
        cases_path,
        docs_dir=tmp_path,
        answer_source="reference-fixture",
        metric_profile="full",
        repeat_count=3,
        human_review_path=None,
    )

    assert judge_execution_status("full")["status"] == "not_run"
    assert payload["summary"]["status"] == "not_run"
    assert payload["summary"]["deterministic_status"] == "passed"
    assert payload["summary"]["faithfulness_avg"] is None
    assert len(payload["case_scores"]) == 1
    assert payload["case_scores"][0]["judge_metrics_status"] == "not_run"


@pytest.mark.asyncio
async def test_full_profile_failed_case_artifact_keeps_judge_diagnostics(tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "redis.md").write_text(
        "Redis connected_clients maxclients incident-window evidence approval.",
        encoding="utf-8",
    )
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis_full
    query: Redis connected_clients maxclients approval
    expected_source: redis.md
    reference_context_ids:
      - redis.md
    reference_answer: Check Redis connected_clients in the incident-window before approval.
    ragas_tags:
      - core_interview
    business_rubric:
      - Redis evidence
      - approval boundary
""",
        encoding="utf-8",
    )

    def runner(samples, runner_context):
        runner_context["judge_diagnostics"].append(
            {
                "case_id": "redis_full",
                "repeat_index": runner_context["repeat_index"],
                "metric": "answer_relevancy",
                "status": "unavailable",
                "raw_value": "nan",
                "raw_value_type": "float",
                "value": None,
                "is_finite": False,
                "duration_ms": 1.0,
                "error": "provider returned NaN",
            }
        )
        return {
            "redis_full": {
                "faithfulness": 1.0,
                "answer_relevancy": None,
                "judge_oncall_actionability": 1.0,
                "answer_completeness": 1.0,
                "id_based_context_precision": 1.0,
                "id_based_context_recall": 1.0,
            }
        }

    payload = await evaluate_cases(
        cases_path,
        docs_dir=docs_dir,
        answer_source="reference-fixture",
        metric_profile="full",
        metrics_runner=runner,
        human_review_path=None,
    )
    failed_path = tmp_path / "ragas_full_core_failed_cases.json"
    write_eval_artifacts(
        payload,
        summary_json_path=tmp_path / "summary.json",
        summary_md_path=tmp_path / "summary.md",
        failed_cases_path=failed_path,
    )

    failed_case = payload["summary"]["failed_cases"][0]
    assert payload["run"]["case_set_sha256"]
    assert "answer_relevancy_unavailable" in failed_case["failed_metrics"]
    assert failed_case["judge_diagnostics"][0]["error"] == "provider returned NaN"
    artifact = json.loads(failed_path.read_text(encoding="utf-8"))
    assert artifact["failed_cases"][0]["answer"]
    assert artifact["failed_cases"][0]["retrieved_contexts"]


def test_ragas_artifacts_replace_non_finite_values_with_null(tmp_path) -> None:
    payload = {
        "run": {},
        "thresholds": {},
        "summary": {"failed_cases": []},
        "case_scores": [{"metrics": {"answer_relevancy": math.nan}}],
    }

    write_eval_artifacts(
        payload,
        summary_json_path=tmp_path / "summary.json",
        summary_md_path=None,
        failed_cases_path=tmp_path / "failed.json",
    )

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["case_scores"][0]["metrics"]["answer_relevancy"] is None


def test_human_review_template_is_blind_and_uses_zero_to_two_scale() -> None:
    payload = {
        "run": {
            "started_at": "2026-07-11T00:00:00+00:00",
            "metric_profile": "id-smoke",
            "answer_source": "reference-fixture",
            "repeat_count": 3,
        },
        "case_scores": [
            {
                "id": "case-a",
                "query": "Redis maxclients?",
                "passed": True,
                "metrics": {"faithfulness": 1.0},
                "repeat_results": [
                    {
                        "repeat_index": 1,
                        "query": "Redis maxclients?",
                        "answer": "Check connected_clients.",
                        "retrieved_contexts": ["runbook"],
                        "citations": [{"source_file": "redis.md", "chunk_id": "redis.md#1"}],
                        "metrics": {"faithfulness": 1.0},
                    }
                ],
            }
        ],
    }

    template = build_human_review_template(payload, reviewer="sre", max_items=30)

    assert template["rubric"]["scale"] == "0-2"
    assert template["items"][0]["rubric_max_score"] == 2
    assert "metrics" not in template["items"][0]
    assert "passed" not in template["items"][0]
    assert "automatic" not in str(template["items"][0]).lower()
    assert template["source_run"]["case_set_sha256"] is None
    assert template["source_run"]["review_item_set_sha256"] == review_item_set_sha256(
        payload["case_scores"]
    )


def test_load_human_reviews_preserves_two_reviewers_for_same_case(tmp_path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text(
        """
{
  "items": [
    {"case_id": "case-a", "reviewer": "one", "decision": "pass"},
    {"case_id": "case-a", "reviewer": "two", "decision": "fail"}
  ]
}
""",
        encoding="utf-8",
    )

    reviews = load_human_reviews(path)
    comparison = compare_human_reviews([{"id": "case-a", "passed": True}], reviews)

    assert len(reviews["case-a"]) == 2
    assert comparison["reviewer_count"] == 2
    assert comparison["inter_rater_agreement"]["status"] == "not_computed"


def test_unbound_human_review_is_invalid_for_evaluated_answers(tmp_path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text(
        """
{
  "source_run": {
    "answer_source": "product-offline",
    "repeat_count": 1
  },
  "items": [
    {"case_id": "case-a", "reviewer": "one", "decision": "pass"}
  ]
}
""",
        encoding="utf-8",
    )

    comparison = compare_human_reviews(
        [{"id": "case-a", "passed": True}],
        load_human_reviews(path),
        expected_source_run={
            "case_set_sha256": "cases",
            "answer_source": "product-offline",
            "repeat_count": 1,
            "review_item_set_sha256": "answers",
        },
    )

    assert comparison["status"] == "invalid"
    assert comparison["agreement_rate"] is None
    assert "missing binding fields" in comparison["reason"]


def test_simulated_review_does_not_publish_human_agreement_rate(tmp_path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text(
        """
{
  "evidence_level": "simulated_review",
  "validity_boundary": "Not independent human evidence.",
  "source_run": {
    "case_set_sha256": "cases",
    "answer_source": "product-offline",
    "repeat_count": 1,
    "review_item_set_sha256": "answers"
  },
  "items": [
    {"case_id": "case-a", "reviewer": "simulated", "decision": "pass"}
  ]
}
""",
        encoding="utf-8",
    )

    comparison = compare_human_reviews(
        [{"id": "case-a", "passed": True}],
        load_human_reviews(path),
        expected_source_run={
            "case_set_sha256": "cases",
            "answer_source": "product-offline",
            "repeat_count": 1,
            "review_item_set_sha256": "answers",
        },
    )

    assert comparison["status"] == "simulated_review"
    assert comparison["reviewed_case_count"] == 1
    assert comparison["agreement_rate"] is None
    assert comparison["automatic_agreement_rate"] is None
