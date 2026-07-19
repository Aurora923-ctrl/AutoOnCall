"""Tests for the bounded real-model acceptance runner."""

import asyncio
import json

import pytest

from scripts.performance import run_real_model_acceptance
from scripts.performance.run_real_model_acceptance import (
    _parse_sse_event,
    _request_summary,
    acceptance_execution_identity,
    acceptance_run_status,
    load_rag_acceptance_cases,
    run_acceptance,
    validate_rag_response,
)


def test_request_summary_reports_stage6_acceptance_and_latency() -> None:
    payload = _request_summary(
        [
            {"passed": True, "latency_ms": 100.0},
            {"passed": True, "latency_ms": 200.0},
            {"passed": False, "latency_ms": 50.0},
        ],
        required=2,
    )

    assert payload["acceptance_status"] == "met"
    assert payload["observed"] == 3
    assert payload["passed"] == 2
    assert payload["failed"] == 1
    assert payload["latency_ms"]["p50"] == 100.0
    assert payload["latency_ms"]["p95"] == 200.0
    assert payload["latency_ms"]["count"] == 3
    assert payload["accepted_latency_ms"]["count"] == 2


def test_request_summary_does_not_treat_zero_required_as_met() -> None:
    payload = _request_summary([], required=0)

    assert payload["acceptance_status"] == "not_run"


def test_real_model_acceptance_execution_identity_uses_response_model() -> None:
    identity = acceptance_execution_identity(
        [
            {
                "request_id": "request-1",
                "request_kind": "rag",
                "details": {"runtime_model": "qwen-max"},
            }
        ]
    )

    assert identity["actual_model"] == "qwen-max"
    assert identity["actual_embedding_model"] == "not_observed"


def test_real_model_rag_acceptance_requires_semantic_contract(tmp_path) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis
    query: Redis maxclients?
    required_sources: [redis.md]
    approved_chunk_ids: [redis.md#0001]
    acceptance:
      answer_policy: answer_with_citations
""",
        encoding="utf-8",
    )
    case = load_rag_acceptance_cases(cases_path)[0]

    assert (
        validate_rag_response(
            {
                "success": True,
                "answer": "Check maxclients.",
                "noAnswer": False,
                "answerPolicy": "answer_with_citations",
                "citations": [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
                "retrieval": {
                    "status": "success",
                    "retrieval_mode": "hybrid_rrf_rerank",
                },
                "observability": {"runtime": {"llm_model": "qwen-max"}},
            },
            case,
        )
        == []
    )
    assert validate_rag_response(
        {
            "success": True,
            "answer": "Check maxclients.",
            "noAnswer": False,
            "answerPolicy": "answer_with_citations",
            "citations": [],
            "retrieval": {
                "status": "success",
                "retrieval_mode": "hybrid_rrf_rerank",
            },
            "observability": {"runtime": {"llm_model": "qwen-max"}},
        },
        case,
    )


def test_real_model_rag_acceptance_rejects_fixture_runtime_evidence(tmp_path) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: redis
    query: Redis maxclients?
    required_sources: [redis.md]
    approved_chunk_ids: [redis.md#0001]
    acceptance:
      answer_policy: answer_with_citations
""",
        encoding="utf-8",
    )
    case = load_rag_acceptance_cases(cases_path)[0]

    errors = validate_rag_response(
        {
            "answer": "Check maxclients.",
            "noAnswer": False,
            "answerPolicy": "answer_with_citations",
            "citations": [{"source_file": "redis.md", "chunk_id": "redis.md#0001"}],
            "retrieval": {
                "status": "success",
                "retrieval_mode": "offline_lexical_fixture",
            },
            "observability": {"runtime": {"llm_model": ""}},
        },
        case,
    )

    assert "retrieval mode does not prove a runtime retrieval" in errors
    assert "runtime LLM model evidence is missing" in errors


def test_real_model_refusal_rejects_citations() -> None:
    case = {
        "should_reject": True,
        "answer_policy": "refuse_without_trusted_source",
        "citations_must_be_empty": True,
        "required_sources": [],
        "approved_chunk_ids": [],
    }

    assert validate_rag_response(
        {
            "answer": "No trusted source.",
            "noAnswer": True,
            "answerPolicy": "refuse_without_trusted_source",
            "citations": [{"source_file": "unrelated.md", "chunk_id": "unrelated.md#1"}],
            "retrieval": {"status": "no_answer", "retrieval_mode": "hybrid_rrf_rerank"},
            "observability": {"runtime": {"llm_model": "qwen-max"}},
        },
        case,
    ) == ["refusal returned citations"]


def test_real_model_refusal_requires_runtime_retrieval_evidence() -> None:
    case = {
        "should_reject": True,
        "answer_policy": "refuse_without_trusted_source",
        "citations_must_be_empty": True,
        "required_sources": [],
        "approved_chunk_ids": [],
    }

    assert validate_rag_response(
        {
            "answer": "No trusted source.",
            "noAnswer": True,
            "answerPolicy": "refuse_without_trusted_source",
            "citations": [],
            "retrieval": {"status": "no_answer", "retrieval_mode": "offline_fixture"},
            "observability": {"runtime": {"llm_model": "qwen-max"}},
        },
        case,
    ) == ["retrieval mode does not prove a runtime retrieval"]


def test_real_model_refusal_requires_runtime_model_evidence() -> None:
    case = {
        "should_reject": True,
        "answer_policy": "refuse_without_trusted_source",
        "citations_must_be_empty": True,
        "required_sources": [],
        "approved_chunk_ids": [],
    }

    assert (
        validate_rag_response(
            {
                "answer": "No trusted source.",
                "noAnswer": True,
                "answerPolicy": "refuse_without_trusted_source",
                "citations": [],
                "retrieval": {"status": "no_answer", "retrieval_mode": "hybrid_rrf_rerank"},
            },
            case,
        )
        == []
    )


def test_real_model_acceptance_requires_approved_chunk_per_required_source() -> None:
    case = {
        "should_reject": False,
        "answer_policy": "answer_with_citations",
        "citations_must_be_empty": False,
        "required_sources": ["redis.md", "postmortem.pdf"],
        "approved_chunk_ids": ["redis.md#0001", "postmortem.pdf#0002"],
    }
    data = {
        "answer": "Check both sources.",
        "noAnswer": False,
        "answerPolicy": "answer_with_citations",
        "citations": [
            {"source_file": "redis.md", "chunk_id": "redis.md#0001"},
            {"source_file": "postmortem.pdf", "chunk_id": "postmortem.pdf#9999"},
        ],
        "retrieval": {"status": "success", "retrieval_mode": "hybrid_rrf_rerank"},
        "observability": {"runtime": {"llm_model": "qwen-max"}},
    }

    assert validate_rag_response(data, case) == [
        "required sources lack approved cited chunks: ['postmortem.pdf']"
    ]


def test_real_model_sse_parser_reads_terminal_status() -> None:
    assert _parse_sse_event('data: {"type":"complete","status":"completed"}') == {
        "type": "complete",
        "status": "completed",
    }


def test_real_model_acceptance_does_not_pass_without_requests(tmp_path) -> None:
    payload = asyncio.run(
        run_acceptance(
            base_url="http://127.0.0.1:1",
            rag_requests=0,
            aiops_requests=0,
            concurrency=1,
            rag_cases_path=tmp_path / "unused.yaml",
        )
    )

    assert payload["summary"]["status"] == "not_run"
    assert payload["summary"]["request_count"] == 0


def test_real_model_acceptance_marks_successful_partial_workload_incomplete() -> None:
    assert (
        acceptance_run_status(
            [{"passed": True, "request_kind": "aiops"}],
            rag_requests=0,
            aiops_requests=1,
        )
        == "incomplete"
    )


def test_real_model_acceptance_requires_dataset_case_coverage() -> None:
    assert (
        acceptance_run_status(
            [
                {"passed": True, "request_kind": "rag"},
                {"passed": True, "request_kind": "aiops"},
            ],
            rag_requests=1,
            aiops_requests=1,
            rag_case_coverage_met=False,
        )
        == "incomplete"
    )


def test_real_model_acceptance_rejects_empty_positive_contract(tmp_path) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: empty-contract
    query: Redis maxclients?
    acceptance:
      answer_policy: answer_with_citations
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required_sources"):
        load_rag_acceptance_cases(cases_path)


def test_real_model_acceptance_marks_unverified_semantic_claim_contract_incomplete(
    tmp_path,
) -> None:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: semantic-contract
    query: Redis maxclients?
    required_sources: [redis.md]
    approved_chunk_ids: [redis.md#0001]
    acceptance:
      answer_policy: answer_with_citations
      required_claims:
        - Keep production changes behind approval.
""",
        encoding="utf-8",
    )

    cases = load_rag_acceptance_cases(cases_path)

    assert cases[0]["required_claims"] == ["Keep production changes behind approval."]
    assert (
        acceptance_run_status(
            [
                {"passed": True, "request_kind": "rag"},
                {"passed": True, "request_kind": "aiops"},
            ],
            rag_requests=1,
            aiops_requests=1,
            semantic_claims_unverified=True,
        )
        == "incomplete"
    )


def test_real_model_acceptance_accepts_safe_degraded_aiops_terminal_state() -> None:
    request = {
        "passed": True,
        "request_kind": "aiops",
        "latency_ms": 1.0,
        "details": {
            "terminal_status": "degraded",
            "degradation_analysis": {
                "category": "evidence_insufficient",
                "safe_terminal": True,
                "needs_human": True,
            },
        },
    }

    summary = _request_summary([request], required=1)

    assert summary["acceptance_status"] == "met"
    assert summary["completed"] == 0
    assert summary["degraded"] == 1
    assert (
        acceptance_run_status(
            [
                request,
                {"passed": True, "request_kind": "rag", "details": {}},
            ],
            rag_requests=1,
            aiops_requests=1,
        )
        == "passed_with_degraded"
    )


@pytest.mark.asyncio
async def test_real_model_acceptance_rejects_unsafe_degraded_aiops_terminal_state() -> None:
    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            yield (
                'data: {"type":"complete","status":"degraded",'
                '"structured_report":{"status":"degraded","degradation_analysis":'
                '{"category":"dependency_timeout","safe_terminal":false,'
                '"needs_human":true}}}'
            )

    class FakeClient:
        def stream(self, *args, **kwargs):
            return FakeStream()

    result = await run_real_model_acceptance._run_aiops(
        FakeClient(),
        base_url="http://testserver",
        run_id="run-unsafe",
        index=1,
    )

    assert result["passed"] is False
    assert result["details"]["acceptance_class"] == "unsafe_degraded"
    assert result["details"]["degradation_analysis"]["category"] == "dependency_timeout"
    assert result["error"] == "degraded AIOps terminal state is not classified as safe"


@pytest.mark.asyncio
async def test_real_model_acceptance_pins_local_live_redis_instance() -> None:
    captured: dict = {}

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_lines(self):
            yield 'data: {"type":"complete","status":"completed"}'

    class FakeClient:
        def stream(self, *args, **kwargs):
            captured.update(json.loads(kwargs["content"]))
            return FakeStream()

    result = await run_real_model_acceptance._run_aiops(
        FakeClient(),
        base_url="http://testserver",
        run_id="run-pinned",
        index=1,
    )

    assert result["passed"] is True
    raw_alert = captured["incident"]["raw_alert"]
    assert raw_alert["evidence_level"] == "local_live"
    assert raw_alert["redis_instance"] == "redis-cluster-prod"
