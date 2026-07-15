"""Optional RAGAS quality evaluation for AutoOnCall RAG answers."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.services.rag_agent_service import RagAgentService
from app.services.rag_answer_policy import (
    build_grounded_question,
    ensure_citation_block,
)
from app.services.rag_read_models import compact_retrieval_payload
from app.services.rag_retrieval_service import (
    retrieve_structured_knowledge,
)
from scripts.eval.eval_environment import collect_eval_environment, provenance_markdown_lines
from scripts.eval.eval_rag_cases import (
    DEFAULT_DOCS_DIR,
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    build_offline_index,
    load_cases,
    search_offline,
)

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / config.ragas_eval_summary_path
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "ragas_eval_summary.md"
DEFAULT_HUMAN_REVIEW_PATH = REPO_ROOT / "eval" / "ragas_cases.review.json"
CORE_TAG = "core_interview"
REFUSAL_TAG = "refusal_boundary"
SUPPORTED_MODES = {"offline", "runtime"}
SUPPORTED_ANSWER_SOURCES = {
    "product-offline",
    "reference-fixture",
    "context-fixture",
    "runtime",
}
SUPPORTED_METRIC_PROFILES = {"full", "id-smoke"}
DEFAULT_METRIC_PROFILE = "id-smoke"
DEFAULT_ANSWER_SOURCE = "product-offline"
RAGAS_METRICS = [
    "faithfulness",
    "answer_relevancy",
    "judge_oncall_actionability",
    "answer_completeness",
    "id_based_context_precision",
    "id_based_context_recall",
]
RAGAS_ID_METRICS = ["id_based_context_precision", "id_based_context_recall"]
JUDGE_METRICS = [
    "faithfulness",
    "answer_relevancy",
    "judge_oncall_actionability",
    "answer_completeness",
]
JUDGE_PROMPT_VERSION = "stage3-ragas-judge-v1"
HUMAN_REVIEW_SCHEMA_VERSION = 3
HUMAN_REVIEW_DIMENSIONS = [
    "faithfulness",
    "relevancy",
    "citation_support",
    "citation_correctness",
    "actionability",
    "completeness",
    "boundary_safety",
]

MetricRunner = Callable[[list["RagasCaseSample"], dict[str, Any]], dict[str, dict[str, Any]]]


@dataclass(slots=True)
class RagasCaseSample:
    """One normalized RAGAS sample plus AutoOnCall-specific metadata."""

    case: dict[str, Any]
    retrieved_contexts: list[str]
    retrieved_context_ids: list[str]
    reference_context_ids: list[str]
    answer: str
    answer_policy: str
    no_answer: bool
    citations: list[dict[str, Any]]
    retrieval: dict[str, Any]


async def evaluate_cases(
    cases_path: str | Path = DEFAULT_CASES_PATH,
    *,
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
    mode: Literal["offline", "runtime"] = "offline",
    answer_source: Literal[
        "product-offline", "reference-fixture", "context-fixture", "runtime"
    ] = DEFAULT_ANSWER_SOURCE,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    max_cases: int | None = None,
    metric_profile: Literal["id-smoke", "full"] = DEFAULT_METRIC_PROFILE,
    metrics_runner: MetricRunner | None = None,
    repeat_count: int = 1,
    human_review_path: str | Path | None = DEFAULT_HUMAN_REVIEW_PATH,
) -> dict[str, Any]:
    """Evaluate RAG cases with optional RAGAS LLM-as-judge metrics."""
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode={mode}; supported={sorted(SUPPORTED_MODES)}")
    if answer_source not in SUPPORTED_ANSWER_SOURCES:
        raise ValueError(
            f"Unsupported answer_source={answer_source}; supported={sorted(SUPPORTED_ANSWER_SOURCES)}"
        )
    if metric_profile not in SUPPORTED_METRIC_PROFILES:
        raise ValueError(
            f"Unsupported metric_profile={metric_profile}; "
            f"supported={sorted(SUPPORTED_METRIC_PROFILES)}"
        )
    if repeat_count <= 0:
        raise ValueError("repeat_count must be greater than zero")

    started_at = datetime.now(UTC)
    timer = time.perf_counter()
    cases = _select_cases(load_cases(cases_path), max_cases=max_cases)
    judge_status = judge_execution_status(metric_profile)
    human_reviews = load_human_reviews(human_review_path)

    offline_index = build_offline_index(docs_dir) if mode == "offline" else None
    agent = (
        RagAgentService(streaming=False)
        if answer_source in {"product-offline", "context-fixture", "runtime"}
        else None
    )
    runner_context = build_runner_context(
        mode=mode,
        answer_source=answer_source,
        top_k=top_k,
        min_score=min_score,
        metric_profile=metric_profile,
    )
    runner_context["case_set_sha256"] = case_set_sha256(cases)
    generation_mode = answer_generation_mode(answer_source)
    judge_requested = metric_profile == "full"
    judge_available = judge_status["status"] == "ready"
    effective_metric_profile = "full" if judge_available else "id-smoke"
    metric_runner = metrics_runner or metric_runner_for_profile(effective_metric_profile)
    repeated_results: list[list[dict[str, Any]]] = []
    first_samples: list[RagasCaseSample] = []
    for repeat_index in range(1, repeat_count + 1):
        runner_context["repeat_index"] = repeat_index
        samples = [
            await build_case_sample(
                case,
                agent=agent,
                offline_index=offline_index,
                mode=mode,
                answer_source=answer_source,
                top_k=top_k,
                min_score=min_score,
            )
            for case in cases
        ]
        if repeat_index == 1:
            first_samples = samples
        quality_samples_for_run = [sample for sample in samples if not is_refusal_case(sample.case)]
        try:
            metric_scores = (
                metric_runner(quality_samples_for_run, runner_context)
                if quality_samples_for_run
                else {}
            )
        except Exception as exc:
            if not judge_requested:
                raise
            judge_status = {
                **judge_status,
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "exception_type": type(exc).__name__,
            }
            judge_available = False
            metric_scores = {
                str(sample.case.get("id")): deterministic_id_scores(sample)
                for sample in quality_samples_for_run
            }
        repeated_results.append(
            [
                {
                    **build_case_result(
                        sample,
                        metric_scores.get(str(sample.case.get("id")), {}),
                        metric_profile=metric_profile,
                        judge_metrics_available=judge_available,
                        judge_diagnostics=judge_diagnostics_for_case(
                            runner_context,
                            str(sample.case.get("id") or ""),
                            repeat_index=repeat_index,
                        ),
                    ),
                    "repeat_index": repeat_index,
                }
                for sample in samples
            ]
        )

    refusal_samples = [sample for sample in first_samples if is_refusal_case(sample.case)]
    quality_samples = [sample for sample in first_samples if not is_refusal_case(sample.case)]
    results = aggregate_repeated_results(repeated_results)
    summary = build_summary(
        results, quality_samples=quality_samples, refusal_samples=refusal_samples
    )
    summary["deterministic_status"] = summary["status"]
    summary["judge_status"] = judge_status["status"]
    if judge_requested and judge_status["status"] == "not_run":
        summary["status"] = "not_run"
        summary["not_run_reason"] = judge_status["reason"]
    elif judge_requested and judge_status["status"] == "failed":
        summary["status"] = "failed"
        summary["judge_failure_reason"] = judge_status["reason"]
    summary["stability"] = build_run_stability(results, repeat_count=repeat_count)
    summary["human_review"] = compare_human_reviews(results, human_reviews)
    quality_contract = build_quality_contract(
        summary,
        results,
        metric_profile=metric_profile,
        judge_status=judge_status["status"],
    )

    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - timer) * 1000, 2),
            "evaluation_scope": (
                "RAGAS quality regression for fixed AutoOnCall RAG cases; "
                f"retrieval={retrieval_evidence_mode(mode)}, generation={generation_mode}; "
                "not a production accuracy claim"
            ),
            "cases_path": str(Path(cases_path)),
            "docs_dir": str(Path(docs_dir)),
            "mode": mode,
            "answer_source": answer_source,
            "answer_generation_mode": generation_mode,
            "retrieval_evidence_mode": retrieval_evidence_mode(mode),
            "metric_profile": metric_profile,
            "repeat_count": repeat_count,
            "target_shape": {
                "recommended_core_case_count": 30,
                "recommended_repeat_count": 3,
                "selected_case_count": len(cases),
            },
            "judge_execution": judge_status,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "judge_temperature": 0,
            "judge_token_usage": {
                "status": (
                    "not_run"
                    if judge_status["status"] == "not_run"
                    else (
                        "unavailable_from_ragas_result"
                        if judge_status["status"] == "ready"
                        else "failed"
                        if judge_status["status"] == "failed"
                        else "not_required"
                    )
                ),
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
            },
            "human_review_path": str(human_review_path) if human_review_path else "",
            "top_k": top_k,
            "min_score": min_score,
            "case_ids": [str(case.get("id", "")) for case in cases],
            "case_set_sha256": runner_context["case_set_sha256"],
            "metrics": metrics_for_profile(metric_profile),
            "supported_metrics": RAGAS_METRICS,
            "ragas_version": safe_package_version("ragas"),
            "datasets_version": safe_package_version("datasets"),
            "judge_model": config.effective_ragas_eval_model,
            "embedding_model": config.effective_ragas_eval_embedding_model,
            "temperature": 0,
            "environment": collect_eval_environment(suite="ragas"),
        },
        "thresholds": ragas_thresholds(),
        "summary": summary,
        "quality_contract": quality_contract,
        "human_review_comparison": summary["human_review"],
        "case_scores": results,
    }


async def build_case_sample(
    case: dict[str, Any],
    *,
    agent: RagAgentService | None,
    offline_index: list[dict[str, Any]] | None,
    mode: Literal["offline", "runtime"],
    answer_source: Literal["product-offline", "reference-fixture", "context-fixture", "runtime"],
    top_k: int,
    min_score: float,
) -> RagasCaseSample:
    """Build one normalized sample from either offline contexts or runtime retrieval."""
    query = str(case.get("query") or "")
    if mode == "offline":
        if offline_index is None:
            raise ValueError("offline_index is required in offline mode")
        retrieved = search_offline(
            offline_index,
            query,
            top_k=top_k,
            min_score=float(case.get("min_score", min_score)),
        )
        retrieval_payload = offline_retrieval_payload(case, query, retrieved, top_k=top_k)
    else:
        retrieval_payload = retrieve_structured_knowledge(query, top_k=top_k)

    if is_refusal_case(case):
        if answer_source in {"product-offline", "runtime"}:
            if agent is None:
                raise ValueError(f"agent is required for {answer_source} answer_source")
            chat_payload = await query_product_behavior(
                agent,
                case,
                query,
                retrieval_payload=retrieval_payload,
                answer_source=answer_source,
            )
            return sample_from_chat_payload(
                case, chat_payload, fallback_retrieval=retrieval_payload
            )
        return refusal_fixture_sample(case, retrieval_payload)

    if answer_source in {"product-offline", "runtime"}:
        if agent is None:
            raise ValueError(f"agent is required for {answer_source} answer_source")
        chat_payload = await query_product_behavior(
            agent,
            case,
            query,
            retrieval_payload=retrieval_payload,
            answer_source=answer_source,
        )
        return sample_from_chat_payload(case, chat_payload, fallback_retrieval=retrieval_payload)

    citations = [
        {
            "source_file": item.get("source_file", ""),
            "chunk_id": item.get("chunk_id", ""),
            "score": item.get("offline_score", item.get("score")),
        }
        for item in retrieval_payload.get("retrieval_results", [])
    ]
    if answer_source == "reference-fixture":
        answer = build_reference_fixture_answer(case, retrieval_payload, citations)
        return RagasCaseSample(
            case=case,
            retrieved_contexts=contexts_from_retrieval(retrieval_payload),
            retrieved_context_ids=context_ids_from_retrieval(retrieval_payload),
            reference_context_ids=reference_context_ids(case),
            answer=answer,
            answer_policy=str(retrieval_payload.get("answer_policy") or "answer_with_citations"),
            no_answer=False,
            citations=citations,
            retrieval=compact_retrieval_payload(retrieval_payload),
        )

    if agent is None:
        raise ValueError("agent is required for context-fixture answer_source")
    grounded_question = build_grounded_question(query, retrieval_payload)
    answer = await agent.query_grounded(
        grounded_question,
        session_id=f"ragas-fixture-{case.get('id', 'case')}",
        history_question=query,
    )
    answer = ensure_citation_block(answer, citations)
    return RagasCaseSample(
        case=case,
        retrieved_contexts=contexts_from_retrieval(retrieval_payload),
        retrieved_context_ids=context_ids_from_retrieval(retrieval_payload),
        reference_context_ids=reference_context_ids(case),
        answer=answer,
        answer_policy=str(retrieval_payload.get("answer_policy") or "answer_with_citations"),
        no_answer=False,
        citations=citations,
        retrieval=compact_retrieval_payload(retrieval_payload),
    )


def offline_retrieval_payload(
    case: dict[str, Any],
    query: str,
    retrieved: list[dict[str, Any]],
    *,
    top_k: int,
) -> dict[str, Any]:
    """Return a structured retrieval payload from deterministic offline chunks."""
    if is_refusal_case(case):
        return {
            "status": "no_answer",
            "query": query,
            "source": "ragas_offline",
            "top_k": top_k,
            "retrieval_mode": "offline_lexical_fixture",
            "retrieval_results": [],
            "rejected_results": retrieved,
            "answer_policy": "refuse_without_trusted_source",
            "no_answer_rejected": True,
            "summary": "offline fixture rejected knowledge-base out-of-scope case",
            "content": "",
        }
    return {
        "status": "success" if retrieved else "no_answer",
        "query": query,
        "source": "ragas_offline",
        "top_k": top_k,
        "retrieval_mode": "offline_lexical_fixture",
        "retrieval_results": retrieved,
        "rejected_results": [],
        "answer_policy": "answer_with_citations" if retrieved else "refuse_without_trusted_source",
        "no_answer_rejected": not bool(retrieved),
        "summary": f"offline fixture retrieved {len(retrieved)} chunks",
        "content": format_offline_context(retrieved),
    }


async def query_product_behavior(
    agent: RagAgentService,
    case: dict[str, Any],
    query: str,
    *,
    retrieval_payload: dict[str, Any],
    answer_source: str,
) -> dict[str, Any]:
    """Run the same public product method while keeping offline retrieval deterministic."""
    if answer_source == "runtime":
        return await agent.query_with_retrieval(
            query,
            session_id=f"ragas-{case.get('id', 'case')}",
        )

    from app.services import rag_agent_service as rag_agent_module

    original_retrieve = rag_agent_module.retrieve_structured_knowledge
    original_query_grounded = agent.query_grounded
    original_query_grounded_observed = agent.query_grounded_observed

    async def fixture_query_grounded(
        _grounded_question: str,
        _session_id: str,
        *,
        history_question: str | None = None,
    ) -> str:
        citations = [
            {
                "source_file": item.get("source_file", ""),
                "chunk_id": item.get("chunk_id", ""),
                "score": item.get("offline_score", item.get("score")),
            }
            for item in retrieval_payload.get("retrieval_results", [])
        ]
        return build_reference_fixture_answer(case, retrieval_payload, citations)

    async def fixture_query_grounded_observed(
        grounded_question: str,
        session_id: str,
        *,
        history_question: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        answer = await fixture_query_grounded(
            grounded_question,
            session_id,
            history_question=history_question,
        )
        return answer, {
            "llm_generation_ms": 0.0,
            "llm_ttft_ms": "not_run",
            "token_usage": {"status": "not_run"},
            "model": "reference-fixture",
        }

    try:
        rag_agent_module.retrieve_structured_knowledge = lambda *_args, **_kwargs: retrieval_payload
        agent.query_grounded = fixture_query_grounded  # type: ignore[method-assign]
        agent.query_grounded_observed = fixture_query_grounded_observed  # type: ignore[method-assign]
        return await agent.query_with_retrieval(
            query,
            session_id=f"ragas-offline-{case.get('id', 'case')}",
        )
    finally:
        rag_agent_module.retrieve_structured_knowledge = original_retrieve
        agent.query_grounded = original_query_grounded  # type: ignore[method-assign]
        agent.query_grounded_observed = original_query_grounded_observed  # type: ignore[method-assign]


def sample_from_chat_payload(
    case: dict[str, Any],
    chat_payload: dict[str, Any],
    *,
    fallback_retrieval: dict[str, Any],
) -> RagasCaseSample:
    """Normalize the production RAG chat payload for RAGAS scoring."""
    retrieval = chat_payload.get("retrieval")
    retrieval_payload = retrieval if isinstance(retrieval, dict) else fallback_retrieval
    citations = chat_payload.get("citations")
    citation_items = citations if isinstance(citations, list) else []
    normalized_citations = [item for item in citation_items if isinstance(item, dict)]
    return RagasCaseSample(
        case=case,
        retrieved_contexts=contexts_for_citations(
            fallback_retrieval,
            normalized_citations,
        ),
        retrieved_context_ids=context_ids_from_retrieval(fallback_retrieval),
        reference_context_ids=reference_context_ids(case),
        answer=str(chat_payload.get("answer") or ""),
        answer_policy=str(
            chat_payload.get("answer_policy") or retrieval_payload.get("answer_policy") or ""
        ),
        no_answer=bool(chat_payload.get("no_answer")),
        citations=normalized_citations,
        retrieval=retrieval_payload,
    )


def build_reference_fixture_answer(
    case: dict[str, Any],
    retrieval_payload: dict[str, Any],
    citations: list[dict[str, Any]],
) -> str:
    """Build a concise deterministic answer fixture for the product retrieval path."""
    answer = str(case.get("reference_answer") or "").strip()
    if not answer:
        answer = build_fallback_reference_answer(case, retrieval_payload)
    answer = "\n\n".join(
        [
            answer,
            (
                "OnCall decision: check metric/log evidence in the incident-window, "
                "confirm the retrieved source_file/chunk_id, and keep approval boundaries "
                "before rollback, scale, limit, or degradation actions."
            ),
        ]
    )
    return ensure_citation_block(answer, citations)


def answer_for_judge(answer: str) -> str:
    """Remove mechanical citation metadata before semantic LLM-as-judge scoring."""
    lines = str(answer or "").splitlines()
    citation_start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^\s*-\s*source_file\s*:", line, flags=re.IGNORECASE)
        ),
        None,
    )
    content = lines if citation_start is None else lines[:citation_start]
    while content and not content[-1].strip():
        content.pop()
    if content and (
        "source" in content[-1].lower() or content[-1].strip().startswith("引用来源")
    ):
        content.pop()
    semantic_answer = "\n".join(content).strip()
    return re.sub(
        r"\[\s*[^]\n|]+\s*\|\s*[^]\n]+\s*\]",
        "",
        semantic_answer,
    ).strip()


def build_fallback_reference_answer(case: dict[str, Any], retrieval_payload: dict[str, Any]) -> str:
    """Build a deterministic answer when a legacy RAG case has no reference answer."""
    query = str(case.get("query") or "the incident").strip()
    expected = ", ".join(expected_sources(case)) or "retrieved runbook"
    keywords = ", ".join(str(item) for item in case.get("expected_keywords", []) or [])
    retrieved = retrieval_payload.get("retrieval_results", []) or []
    top_source = ""
    if retrieved and isinstance(retrieved[0], dict):
        top_source = str(retrieved[0].get("source_file") or "")
    source_hint = top_source or expected
    return (
        f"For query `{query}`, use {source_hint} as the trusted runbook source. "
        f"Key evidence terms: {keywords or expected}. "
        "Separate runbook knowledge from live incident evidence, then check metrics/logs "
        "and choose rollback, scale, limit, or degradation only inside approval boundaries."
    )


def refusal_fixture_sample(
    case: dict[str, Any],
    retrieval_payload: dict[str, Any],
) -> RagasCaseSample:
    """Build a deterministic refusal sample for out-of-scope smoke cases."""
    answer = (
        "Cannot answer from the trusted AutoOnCall knowledge base: the question is out "
        "of scope or lacks a trusted source. Please add documentation before using RAG."
    )
    return RagasCaseSample(
        case=case,
        retrieved_contexts=[],
        retrieved_context_ids=[],
        reference_context_ids=reference_context_ids(case),
        answer=answer,
        answer_policy="refuse_without_trusted_source",
        no_answer=True,
        citations=[],
        retrieval=compact_retrieval_payload(retrieval_payload),
    )


def run_ragas_metrics(
    samples: list[RagasCaseSample],
    runner_context: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Run Judge metrics independently so one provider failure stays diagnosable."""
    if not samples:
        return {}
    try:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample

        try:
            from ragas.metrics.collections import (
                AspectCritic,
                Faithfulness,
                IDBasedContextPrecision,
                IDBasedContextRecall,
                ResponseRelevancy,
            )
        except ImportError:
            from ragas.metrics import (
                _AspectCritic as AspectCritic,
                _Faithfulness as Faithfulness,
                _IDBasedContextPrecision as IDBasedContextPrecision,
                _IDBasedContextRecall as IDBasedContextRecall,
                _ResponseRelevancy as ResponseRelevancy,
            )
    except Exception as exc:
        raise RuntimeError(f"RAGAS dependencies are unavailable: {exc}") from exc

    if not config.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required for RAGAS judge metrics")

    dataset = EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=str(sample.case.get("query") or ""),
                retrieved_contexts=sample.retrieved_contexts,
                retrieved_context_ids=sample.retrieved_context_ids,
                reference_context_ids=sample.reference_context_ids,
                response=answer_for_judge(sample.answer),
                reference=str(sample.case.get("reference_answer") or ""),
                rubrics=rubrics_from_case(sample.case),
            )
            for sample in samples
        ]
    )
    llm = ChatOpenAI(
        model=runner_context["judge_model"],
        api_key=config.dashscope_api_key,
        base_url=config.dashscope_api_base,
        temperature=0,
        timeout=60,
        max_retries=2,
    )
    embeddings = OpenAIEmbeddings(
        model=runner_context["embedding_model"],
        api_key=config.dashscope_api_key,
        base_url=config.dashscope_api_base,
        timeout=60,
        max_retries=2,
        # DashScope's OpenAI-compatible embedding endpoint accepts strings, but
        # rejects the token-id lists produced by LangChain's tiktoken safety path.
        check_embedding_ctx_length=False,
    )
    metric_factories: dict[str, Callable[[], Any]] = {
        "faithfulness": Faithfulness,
        "answer_relevancy": lambda: ResponseRelevancy(strictness=1),
        "judge_oncall_actionability": lambda: AspectCritic(
            name="judge_oncall_actionability",
            definition=(
                "The response gives concrete incident evidence checks and bounded OnCall "
                "actions, preserving approval, dry-run, rollback, and human takeover boundaries."
            ),
            strictness=3,
        ),
        "answer_completeness": lambda: AspectCritic(
            name="answer_completeness",
            definition=(
                "The response covers the important facts, evidence checks, citations, "
                "remediation options, and safety boundaries needed to answer the user."
            ),
            strictness=3,
        ),
        "id_based_context_precision": IDBasedContextPrecision,
        "id_based_context_recall": IDBasedContextRecall,
    }
    scores = {str(sample.case.get("id")): {} for sample in samples}
    diagnostics = runner_context.setdefault("judge_diagnostics", [])
    repeat_index = int(runner_context.get("repeat_index") or 1)

    for metric_name, metric_factory in metric_factories.items():
        started_at = time.perf_counter()
        try:
            result = evaluate(
                dataset,
                metrics=[metric_factory()],
                llm=llm,
                embeddings=embeddings if metric_name == "answer_relevancy" else None,
                raise_exceptions=False,
                show_progress=False,
            )
            rows = result.to_pandas().to_dict(orient="records")
            for sample, row in zip(samples, rows, strict=False):
                case_id = str(sample.case.get("id"))
                raw_value = row.get(metric_name)
                value = optional_metric_float(raw_value)
                scores[case_id][metric_name] = value
                diagnostics.append(
                    judge_metric_diagnostic(
                        case_id=case_id,
                        repeat_index=repeat_index,
                        metric=metric_name,
                        raw_value=raw_value,
                        value=value,
                        duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                    )
                )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for sample in samples:
                case_id = str(sample.case.get("id"))
                scores[case_id][metric_name] = None
                diagnostics.append(
                    judge_metric_diagnostic(
                        case_id=case_id,
                        repeat_index=repeat_index,
                        metric=metric_name,
                        raw_value=None,
                        value=None,
                        duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                        error=reason,
                    )
                )
    return scores


def metric_runner_for_profile(metric_profile: str) -> MetricRunner:
    """Return the metric runner for a reproducibility profile."""
    if metric_profile == "id-smoke":
        return run_ragas_id_smoke_metrics
    if metric_profile == "full":
        return run_ragas_metrics
    raise ValueError(f"Unsupported metric_profile={metric_profile}")


def metrics_for_profile(metric_profile: str) -> list[str]:
    """Return metrics that are expected to be populated by a profile."""
    if metric_profile == "id-smoke":
        return [*RAGAS_ID_METRICS, "oncall_actionability_score"]
    return RAGAS_METRICS


def run_ragas_id_smoke_metrics(
    samples: list[RagasCaseSample],
    runner_context: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """Run RAGAS ID metrics without an external judge model.

    This keeps the interview/demo path reproducible: the same fixed cases can
    prove retrieval grounding and business gates even when no judge API key is
    configured. If the RAGAS API changes, the deterministic fallback keeps the
    CI signal available while still reporting the installed RAGAS version.
    """
    if not samples:
        return {}
    try:
        import warnings

        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample

        try:
            from ragas.metrics.collections import IDBasedContextPrecision, IDBasedContextRecall
        except Exception:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall

        dataset = EvaluationDataset(
            samples=[
                SingleTurnSample(
                    user_input=str(sample.case.get("query") or ""),
                    retrieved_contexts=sample.retrieved_contexts,
                    retrieved_context_ids=sample.retrieved_context_ids,
                    reference_context_ids=sample.reference_context_ids,
                    response=sample.answer,
                    reference=str(sample.case.get("reference_answer") or ""),
                )
                for sample in samples
            ]
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = evaluate(
                dataset,
                metrics=[IDBasedContextPrecision(), IDBasedContextRecall()],
                raise_exceptions=False,
                show_progress=False,
            )
        rows = result.to_pandas().to_dict(orient="records")
        return {
            str(sample.case.get("id")): {
                metric: safe_float(row.get(metric))
                for metric in RAGAS_ID_METRICS
                if row.get(metric) is not None
            }
            for sample, row in zip(samples, rows, strict=False)
        }
    except Exception:
        return {str(sample.case.get("id")): deterministic_id_scores(sample) for sample in samples}


def deterministic_id_scores(sample: RagasCaseSample) -> dict[str, float]:
    """Compute ID precision/recall locally as a stable smoke fallback."""
    retrieved = set(sample.retrieved_context_ids)
    reference = set(sample.reference_context_ids)
    hits = retrieved & reference
    return {
        "id_based_context_precision": ratio(len(hits), len(retrieved)),
        "id_based_context_recall": ratio(len(hits), len(reference)),
    }


def judge_execution_status(metric_profile: str) -> dict[str, str]:
    """Describe whether judge-backed metrics can run without hiding missing credentials."""
    if metric_profile == "id-smoke":
        return {
            "status": "not_required",
            "reason": "id-smoke uses deterministic ID and business metrics",
        }
    if not config.dashscope_api_key:
        return {
            "status": "not_run",
            "reason": "DASHSCOPE_API_KEY is not configured; full judge metrics were not executed",
        }
    return {
        "status": "ready",
        "reason": "judge credentials are configured",
        "model": config.effective_ragas_eval_model,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "temperature": "0",
    }


def build_not_run_payload(
    *,
    cases: list[dict[str, Any]],
    cases_path: str | Path,
    docs_dir: str | Path,
    mode: str,
    answer_source: str,
    top_k: int,
    min_score: float,
    metric_profile: str,
    repeat_count: int,
    human_review_path: str | Path | None,
    human_reviews: dict[str, list[dict[str, Any]]],
    started_at: datetime,
    timer: float,
    judge_status: dict[str, str],
) -> dict[str, Any]:
    """Return an honest artifact when a requested full judge run cannot execute."""
    summary = {
        "status": "not_run",
        "case_count": len(cases),
        "quality_case_count": sum(1 for case in cases if not is_refusal_case(case)),
        "refusal_case_count": sum(1 for case in cases if is_refusal_case(case)),
        "passed_count": 0,
        "pass_rate": None,
        "core_case_count": sum(1 for case in cases if is_core_case(case)),
        "core_case_pass_rate": None,
        "refusal_boundary_rate": None,
        "faithfulness_avg": None,
        "response_relevancy_avg": None,
        "id_context_precision_avg": None,
        "id_context_recall_avg": None,
        "oncall_actionability_avg": None,
        "citation_grounding_rate": None,
        "citation_existence_rate": None,
        "citation_support_rate": None,
        "citation_correctness_rate": None,
        "factual_error_rate": None,
        "severe_hallucination_rate": None,
        "incident_boundary_rate": None,
        "confusion_disambiguation_rate": None,
        "stability": {
            "repeat_count": repeat_count,
            "status": "not_run",
            "reason": judge_status["reason"],
        },
        "human_review": compare_human_reviews([], human_reviews),
        "failed_cases": [],
        "not_run_reason": judge_status["reason"],
    }
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - timer) * 1000, 2),
            "evaluation_scope": "full RAGAS judge evaluation was requested but not executed",
            "cases_path": str(Path(cases_path)),
            "docs_dir": str(Path(docs_dir)),
            "mode": mode,
            "answer_source": answer_source,
            "metric_profile": metric_profile,
            "repeat_count": repeat_count,
            "target_shape": {
                "recommended_core_case_count": 30,
                "recommended_repeat_count": 3,
                "selected_case_count": len(cases),
            },
            "judge_execution": judge_status,
            "human_review_path": str(human_review_path) if human_review_path else "",
            "top_k": top_k,
            "min_score": min_score,
            "case_ids": [str(case.get("id", "")) for case in cases],
            "metrics": metrics_for_profile(metric_profile),
            "supported_metrics": RAGAS_METRICS,
            "ragas_version": safe_package_version("ragas"),
            "datasets_version": safe_package_version("datasets"),
            "judge_model": config.effective_ragas_eval_model,
            "embedding_model": config.effective_ragas_eval_embedding_model,
            "temperature": 0,
            "environment": collect_eval_environment(suite="ragas"),
        },
        "thresholds": ragas_thresholds(),
        "summary": summary,
        "quality_contract": {
            "name": "AutoOnCall RAGAS business quality contract",
            "status": "not_run",
            "profile": metric_profile,
            "hard_gates": [],
            "watch_metrics": [],
            "risk_register": [],
            "interview_talk_track": [
                "The full judge profile is reported as not_run when credentials are absent; "
                "it is never converted into a zero score or a false pass."
            ],
        },
        "human_review_comparison": summary["human_review"],
        "case_scores": [],
    }


def aggregate_repeated_results(
    repeated_results: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Aggregate repeated case executions while retaining every raw result."""
    if not repeated_results:
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for run_results in repeated_results:
        for result in run_results:
            case_id = str(result.get("id") or "unknown")
            if case_id not in grouped:
                grouped[case_id] = []
                order.append(case_id)
            grouped[case_id].append(result)

    aggregated = []
    for case_id in order:
        runs = grouped[case_id]
        base = dict(runs[0])
        base.pop("repeat_index", None)
        pass_values = [1.0 if run.get("passed") else 0.0 for run in runs]
        metric_names = sorted(
            {
                name
                for run in runs
                for name, value in (run.get("metrics") or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        )
        metric_stability = {
            name: optional_numeric_stability([(run.get("metrics") or {}).get(name) for run in runs])
            for name in metric_names
        }
        base["passed"] = all(bool(run.get("passed")) for run in runs)
        base["failed_metrics"] = sorted(
            {metric for run in runs for metric in run.get("failed_metrics", [])}
        )
        base["failure_reasons"] = quality_failure_reasons(base["failed_metrics"])
        base["metrics"] = {
            name: (metric_stability[name]["mean"] if metric_stability[name] is not None else None)
            for name in metric_names
        } | {
            name: value
            for name, value in (runs[0].get("metrics") or {}).items()
            if isinstance(value, bool)
        }
        base["repeat_results"] = runs
        base["stability"] = {
            "repeat_count": len(runs),
            "pass_rate": round(sum(pass_values) / len(pass_values), 4),
            "all_pass": base["passed"],
            "metrics": metric_stability,
        }
        aggregated.append(base)
    return aggregated


def numeric_stability(values: list[float]) -> dict[str, float]:
    """Return mean, population standard deviation, and worst observed value."""
    if not values:
        return {"mean": 0.0, "std": 0.0, "worst": 0.0}
    return {
        "mean": round(statistics.fmean(values), 4),
        "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "worst": round(min(values), 4),
    }


def optional_numeric_stability(values: list[Any]) -> dict[str, float] | None:
    """Return stability only when a metric was actually executed."""
    numeric = [safe_float(value) for value in values if value is not None]
    return numeric_stability(numeric) if numeric else None


def build_run_stability(
    results: list[dict[str, Any]],
    *,
    repeat_count: int,
) -> dict[str, Any]:
    """Summarize repeat stability across all selected cases."""
    pass_rates = [
        safe_float((result.get("stability") or {}).get("pass_rate")) for result in results
    ]
    return {
        "repeat_count": repeat_count,
        "case_count": len(results),
        "all_cases_all_pass": all(
            bool((result.get("stability") or {}).get("all_pass")) for result in results
        ),
        "case_pass_rate": numeric_stability(pass_rates),
        "unstable_case_ids": [
            result["id"]
            for result in results
            if safe_float((result.get("stability") or {}).get("pass_rate")) not in {0.0, 1.0}
        ],
    }


def load_human_reviews(path: str | Path | None) -> dict[str, list[dict[str, Any]]]:
    """Load optional human rubric scores keyed by case id."""
    if not path:
        return {}
    review_path = Path(path)
    if not review_path.exists():
        return {}
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    items = payload.get("items", []) if isinstance(payload, dict) else []
    reviews: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            reviews.setdefault(case_id, []).append(item)
    return reviews


def compare_human_reviews(
    results: list[dict[str, Any]],
    human_reviews: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compare deterministic/LLM results with imported human rubric decisions."""
    result_by_id = {str(result.get("id")): result for result in results}
    comparisons = []
    reviewer_ids: set[str] = set()
    for case_id, reviews_value in human_reviews.items():
        result = result_by_id.get(case_id)
        if result is None:
            continue
        reviews = reviews_value if isinstance(reviews_value, list) else [reviews_value]
        for review in reviews:
            if not isinstance(review, dict):
                continue
            human_pass = human_review_pass(review)
            auto_pass = bool(result.get("passed"))
            reviewer = str(review.get("reviewer") or "").strip()
            if reviewer:
                reviewer_ids.add(reviewer)
            comparisons.append(
                {
                    "case_id": case_id,
                    "run_index": int(review.get("run_index") or 1),
                    "reviewer": reviewer,
                    "human_pass": human_pass,
                    "automatic_pass": auto_pass,
                    "agreement": human_pass == auto_pass if human_pass is not None else None,
                    "rubric_scores": review.get("rubric_scores", {}),
                    "factual_errors": review.get("factual_errors", []),
                    "severe_hallucination": bool(review.get("severe_hallucination")),
                    "notes": str(review.get("notes") or ""),
                }
            )
    decidable = [item for item in comparisons if item["agreement"] is not None]
    reviewer_count = len(reviewer_ids)
    return {
        "status": "available" if decidable else "not_run",
        "reviewed_case_count": len(decidable),
        "reviewed_unique_case_count": len({item["case_id"] for item in decidable}),
        "available_review_count": sum(
            len(items) if isinstance(items, list) else 1 for items in human_reviews.values()
        ),
        "reviewer_count": reviewer_count,
        "automatic_agreement_rate": (
            ratio(
                sum(1 for item in decidable if item["agreement"]),
                len(decidable),
            )
            if decidable
            else None
        ),
        "agreement_rate": (
            ratio(
                sum(1 for item in decidable if item["agreement"]),
                len(decidable),
            )
            if decidable
            else None
        ),
        "inter_rater_agreement": {
            "status": "not_applicable" if reviewer_count < 2 else "not_computed",
            "reason": (
                "Only one reviewer is present; no inter-rater agreement is claimed."
                if reviewer_count < 2
                else "Multiple reviewers are present; pairwise review assignment is required."
            ),
            "cohens_kappa": None,
        },
        "comparisons": comparisons,
    }


def human_review_pass(review: dict[str, Any]) -> bool | None:
    """Derive a human pass decision from an explicit decision or rubric scores."""
    decision = str(review.get("decision") or "").strip().lower()
    if decision in {"pass", "passed", "approve", "approved"}:
        return True
    if decision in {"fail", "failed", "reject", "rejected"}:
        return False
    scores = review.get("rubric_scores")
    if not isinstance(scores, dict) or not scores:
        return None
    numeric = [
        float(value)
        for value in scores.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numeric:
        return None
    max_score = int(review.get("rubric_max_score") or 2)
    if max_score not in {2, 3} or any(value < 0 or value > max_score for value in numeric):
        return None
    if review.get("severe_hallucination") or review.get("factual_errors"):
        return False
    return statistics.fmean(numeric) / max_score >= 0.8


def build_human_review_template(
    payload: dict[str, Any],
    *,
    reviewer: str = "",
    max_items: int = 30,
) -> dict[str, Any]:
    """Export blind rubric items without automatic or judge scores."""
    items: list[dict[str, Any]] = []
    for result in payload.get("case_scores", []):
        if len(items) >= max_items:
            break
        repeats = result.get("repeat_results") or [result]
        for repeat in repeats:
            if len(items) >= max_items:
                break
            items.append(
                {
                    "case_id": str(result.get("id") or ""),
                    "run_index": int(repeat.get("repeat_index") or 1),
                    "reviewer": reviewer,
                    "reviewed_at": "",
                    "query": str(repeat.get("query") or result.get("query") or ""),
                    "answer": str(repeat.get("answer") or ""),
                    "retrieved_contexts": repeat.get("retrieved_contexts", []),
                    "citations": repeat.get("citations", []),
                    "decision": "",
                    "rubric_max_score": 2,
                    "rubric_scores": dict.fromkeys(HUMAN_REVIEW_DIMENSIONS),
                    "factual_errors": [],
                    "severe_hallucination": False,
                    "notes": "",
                }
            )
    return {
        "schema_version": HUMAN_REVIEW_SCHEMA_VERSION,
        "description": (
            "Blind human answer-quality review. Automatic and Judge scores are intentionally "
            "excluded from each item."
        ),
        "source_run": {
            "started_at": payload.get("run", {}).get("started_at"),
            "metric_profile": payload.get("run", {}).get("metric_profile"),
            "answer_source": payload.get("run", {}).get("answer_source"),
            "repeat_count": payload.get("run", {}).get("repeat_count"),
            "prompt_version": payload.get("run", {}).get(
                "judge_prompt_version", JUDGE_PROMPT_VERSION
            ),
        },
        "rubric": {
            "scale": "0-2",
            "labels": {
                "0": "fails or is unsafe",
                "1": "partially meets the criterion",
                "2": "fully meets the criterion",
            },
            "dimensions": HUMAN_REVIEW_DIMENSIONS,
            "pass_rule": (
                "Explicit decision wins; otherwise mean score must be at least 80%, "
                "with no factual error and no severe hallucination."
            ),
        },
        "items": items,
    }


def write_human_review_template(
    payload: dict[str, Any],
    path: str | Path,
    *,
    reviewer: str = "",
    max_items: int = 30,
) -> str:
    """Write a blind human review artifact into eval/ or another explicit path."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_human_review_template(payload, reviewer=reviewer, max_items=max_items),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(output_path)


def build_case_result(
    sample: RagasCaseSample,
    scores: dict[str, Any],
    *,
    metric_profile: str = "full",
    judge_metrics_available: bool = True,
    judge_diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one case score record and apply AutoOnCall gates."""
    case = sample.case
    case_id = str(case.get("id") or "unknown")
    if is_refusal_case(case):
        refusal_hit = refusal_boundary_hit(sample)
        failed_metrics = [] if refusal_hit else ["refusal_boundary"]
        failure_reasons = (
            {}
            if refusal_hit
            else {
                "refusal_boundary": (
                    "Out-of-scope RAG question should return no_answer with refusal policy "
                    "and no citations."
                )
            }
        )
        return {
            "id": case_id,
            "query": str(case.get("query") or ""),
            "case_type": str(case.get("case_type") or "negative"),
            "tags": ragas_tags(case),
            "core_case": is_core_case(case),
            "should_reject": True,
            "passed": refusal_hit,
            "metrics": {
                "refusal_boundary_hit": refusal_hit,
                "citation_grounding_hit": len(sample.citations) == 0,
                "citation_existence_hit": 1.0 if not sample.citations else 0.0,
                "citation_support_score": 1.0 if not sample.citations else 0.0,
                "citation_correctness_score": 1.0 if not sample.citations else 0.0,
                "factual_error_hit": 0.0,
                "severe_hallucination_hit": (
                    1.0 if deterministic_severe_hallucination(sample) else 0.0
                ),
            },
            "failed_metrics": failed_metrics,
            "failure_reasons": failure_reasons,
            "answer_policy": sample.answer_policy,
            "answer": sample.answer,
            "retrieved_contexts": sample.retrieved_contexts,
            "citations": sample.citations,
            "prompt_version": JUDGE_PROMPT_VERSION,
            "retrieved_context_ids": sample.retrieved_context_ids,
            "reference_context_ids": sample.reference_context_ids,
            "retrieved_sources": retrieved_sources(sample),
            "expected_sources": expected_sources(case),
            "suggested_backlog_category": "hallucination_risk" if failed_metrics else "",
            "judge_diagnostics": judge_diagnostics or [],
        }

    business_scores = business_metric_scores(sample)
    metrics = {
        **{
            metric: (
                optional_metric_float(scores.get(metric))
                if metric not in JUDGE_METRICS or judge_metrics_available
                else None
            )
            for metric in RAGAS_METRICS
        },
        **business_scores,
    }
    failed_metrics = failed_quality_metrics(
        metrics,
        sample,
        metric_profile=metric_profile,
        judge_metrics_available=judge_metrics_available,
    )
    unavailable_judge_metrics = [
        metric
        for metric in JUDGE_METRICS
        if metric_profile == "full" and judge_metrics_available and metrics.get(metric) is None
    ]
    failed_metrics.extend(
        f"{metric}_unavailable"
        for metric in unavailable_judge_metrics
        if f"{metric}_unavailable" not in failed_metrics
    )
    return {
        "id": case_id,
        "query": str(case.get("query") or ""),
        "case_type": str(case.get("case_type") or "positive"),
        "tags": ragas_tags(case),
        "core_case": is_core_case(case),
        "should_reject": False,
        "passed": not failed_metrics,
        "judge_metrics_status": (
            "failed"
            if unavailable_judge_metrics
            else "available"
            if judge_metrics_available
            else "not_run"
        ),
        "metrics": metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": quality_failure_reasons(failed_metrics),
        "answer_policy": sample.answer_policy,
        "answer": sample.answer,
        "retrieved_contexts": sample.retrieved_contexts,
        "citations": sample.citations,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "retrieved_context_ids": sample.retrieved_context_ids,
        "reference_context_ids": sample.reference_context_ids,
        "retrieved_sources": retrieved_sources(sample),
        "expected_sources": expected_sources(case),
        "suggested_backlog_category": suggested_backlog_category(failed_metrics),
        "judge_diagnostics": judge_diagnostics or [],
    }


def build_summary(
    results: list[dict[str, Any]],
    *,
    quality_samples: list[RagasCaseSample],
    refusal_samples: list[RagasCaseSample],
) -> dict[str, Any]:
    """Build aggregate RAGAS quality summary with core-case gates."""
    quality_results = [result for result in results if not result["should_reject"]]
    refusal_results = [result for result in results if result["should_reject"]]
    core_results = [result for result in results if result.get("core_case")]
    failed_cases = [
        {
            "suite": "ragas",
            "id": result["id"],
            "failed_metrics": result["failed_metrics"],
            "failure_reasons": result["failure_reasons"],
            "retrieved_sources": result.get("retrieved_sources", []),
            "expected_sources": result.get("expected_sources", []),
            "suggested_backlog_category": result.get("suggested_backlog_category", ""),
            "judge_model": config.effective_ragas_eval_model,
            "query": result.get("query", ""),
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "retrieved_contexts": result.get("retrieved_contexts", []),
            "retrieved_context_ids": result.get("retrieved_context_ids", []),
            "reference_context_ids": result.get("reference_context_ids", []),
            "judge_diagnostics": result.get("judge_diagnostics", []),
            "repeat_results": [
                {
                    "repeat_index": item.get("repeat_index"),
                    "failed_metrics": item.get("failed_metrics", []),
                    "metrics": item.get("metrics", {}),
                    "judge_diagnostics": item.get("judge_diagnostics", []),
                }
                for item in result.get("repeat_results", [])
            ],
        }
        for result in results
        if not result["passed"]
    ]
    status = "passed" if not failed_cases else "failed"
    return {
        "status": status,
        "case_count": len(results),
        "quality_case_count": len(quality_samples),
        "refusal_case_count": len(refusal_samples),
        "passed_count": sum(1 for result in results if result["passed"]),
        "pass_rate": ratio(sum(1 for result in results if result["passed"]), len(results)),
        "core_case_count": len(core_results),
        "core_case_pass_rate": ratio(
            sum(1 for result in core_results if result["passed"]),
            len(core_results),
        ),
        "refusal_boundary_rate": ratio(
            sum(1 for result in refusal_results if result["passed"]),
            len(refusal_results),
        ),
        "faithfulness_avg": average_optional_metric(quality_results, "faithfulness"),
        "response_relevancy_avg": average_optional_metric(quality_results, "answer_relevancy"),
        "judge_oncall_actionability_avg": average_optional_metric(
            quality_results, "judge_oncall_actionability"
        ),
        "answer_completeness_avg": average_optional_metric(quality_results, "answer_completeness"),
        "id_context_precision_avg": average_metric(
            quality_results,
            "id_based_context_precision",
        ),
        "id_context_recall_avg": average_metric(quality_results, "id_based_context_recall"),
        "oncall_actionability_avg": average_metric(quality_results, "oncall_actionability_score"),
        "citation_grounding_rate": average_metric(quality_results, "citation_grounding_hit"),
        "citation_existence_rate": average_metric(quality_results, "citation_existence_hit"),
        "citation_support_rate": average_metric(quality_results, "citation_support_score"),
        "citation_correctness_rate": average_metric(quality_results, "citation_correctness_score"),
        "factual_error_rate": average_metric(quality_results, "factual_error_hit"),
        "severe_hallucination_rate": average_metric(quality_results, "severe_hallucination_hit"),
        "incident_boundary_rate": average_metric(quality_results, "incident_boundary_hit"),
        "confusion_disambiguation_rate": average_metric(
            quality_results,
            "confusion_disambiguation_hit",
        ),
        "failed_cases": failed_cases,
    }


def build_quality_contract(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    metric_profile: str,
    judge_status: str = "ready",
) -> dict[str, Any]:
    """Translate RAGAS metrics into AutoOnCall business quality gates."""
    thresholds = ragas_thresholds()
    quality_count = int(summary.get("quality_case_count", 0) or 0)
    refusal_count = int(summary.get("refusal_case_count", 0) or 0)
    hard_gates = [
        quality_gate(
            "all_cases_pass",
            "All fixed cases pass",
            summary.get("pass_rate"),
            1.0,
            "summary.pass_rate",
            "Avoids average scores hiding a broken incident or refusal case.",
        ),
        quality_gate(
            "core_case_pass_rate",
            "Core interview cases pass",
            summary.get("core_case_pass_rate"),
            thresholds["core_case_pass_rate"],
            "summary.core_case_pass_rate",
            "Redis, MySQL, dependency timeout, confusion, refusal, and multi-source demos must pass.",
        ),
        quality_gate(
            "id_context_recall",
            "Trusted-source recall",
            summary.get("id_context_recall_avg"),
            thresholds["id_context_recall"],
            "summary.id_context_recall_avg",
            "The answer must retrieve the expected runbook, wiki, ticket, or postmortem source.",
            applicable=quality_count > 0,
        ),
        quality_gate(
            "oncall_actionability",
            "OnCall actionability",
            summary.get("oncall_actionability_avg"),
            thresholds["oncall_actionability"],
            "summary.oncall_actionability_avg",
            "Answers must mention incident evidence and bounded remediation actions.",
            applicable=quality_count > 0,
        ),
        quality_gate(
            "citation_grounding",
            "Auditable citations",
            summary.get("citation_grounding_rate"),
            1.0,
            "summary.citation_grounding_rate",
            "Successful answers must expose source_file and chunk_id for review.",
            applicable=quality_count > 0,
        ),
        quality_gate(
            "incident_boundary",
            "Runbook/live-evidence boundary",
            summary.get("incident_boundary_rate"),
            1.0,
            "summary.incident_boundary_rate",
            "The answer must not present static runbook text as live incident evidence.",
            applicable=quality_count > 0,
        ),
        quality_gate(
            "refusal_boundary",
            "Trusted-source refusal boundary",
            summary.get("refusal_boundary_rate"),
            thresholds["refusal_boundary_rate"],
            "summary.refusal_boundary_rate",
            "Out-of-scope questions must refuse instead of hallucinating remediation.",
            applicable=refusal_count > 0,
        ),
    ]
    if metric_profile == "full" and judge_status == "ready":
        hard_gates.extend(
            [
                quality_gate(
                    "faithfulness",
                    "RAGAS faithfulness",
                    summary.get("faithfulness_avg"),
                    thresholds["faithfulness"],
                    "summary.faithfulness_avg",
                    "LLM-as-judge support check against retrieved context.",
                    applicable=quality_count > 0,
                ),
                quality_gate(
                    "response_relevancy",
                    "RAGAS response relevancy",
                    summary.get("response_relevancy_avg"),
                    thresholds["response_relevancy"],
                    "summary.response_relevancy_avg",
                    "LLM-as-judge focus check for the incident question.",
                    applicable=quality_count > 0,
                ),
                quality_gate(
                    "judge_oncall_actionability",
                    "Judge OnCall actionability",
                    summary.get("judge_oncall_actionability_avg"),
                    thresholds["oncall_actionability"],
                    "summary.judge_oncall_actionability_avg",
                    "LLM-as-judge check for concrete, bounded incident actions.",
                    applicable=quality_count > 0,
                ),
                quality_gate(
                    "answer_completeness",
                    "Judge answer completeness",
                    summary.get("answer_completeness_avg"),
                    1.0,
                    "summary.answer_completeness_avg",
                    "LLM-as-judge check that evidence, citations, actions, and boundaries are covered.",
                    applicable=quality_count > 0,
                ),
                quality_gate(
                    "id_context_precision",
                    "Trusted-source precision",
                    summary.get("id_context_precision_avg"),
                    thresholds["id_context_precision"],
                    "summary.id_context_precision_avg",
                    "Full profile gates noisy retrieval once judge metrics are also enabled.",
                    applicable=quality_count > 0,
                ),
            ]
        )
    watch_metrics = build_watch_metrics(
        summary,
        metric_profile=metric_profile,
        judge_status=judge_status,
    )
    contract_passed = all(gate["status"] in {"passed", "not_applicable"} for gate in hard_gates)
    return {
        "name": "AutoOnCall RAGAS business quality contract",
        "status": (
            "not_run"
            if metric_profile == "full" and judge_status == "not_run"
            else "passed"
            if summary.get("status") == "passed" and contract_passed
            else "failed"
        ),
        "profile": metric_profile,
        "case_mix": {
            "total": int(summary.get("case_count", 0) or 0),
            "quality": quality_count,
            "refusal": refusal_count,
            "core": int(summary.get("core_case_count", 0) or 0),
        },
        "hard_gates": hard_gates,
        "watch_metrics": watch_metrics,
        "risk_register": build_quality_risk_register(results, watch_metrics=watch_metrics),
        "interview_talk_track": [
            (
                "I keep deterministic retrieval eval and RAGAS answer-quality eval separate: "
                "the first proves trusted-source recall and citation behavior; this contract "
                "checks grounding, refusal boundaries, and OnCall actionability."
            ),
            (
                "The default product-offline mode still calls RagAgentService.query_with_retrieval, "
                "but injects deterministic offline retrieval so CI and interviews do not require Milvus."
            ),
            (
                "Precision is watch-only in id-smoke because Top-K can include useful extra context; "
                "core case pass rate, recall, citations, refusal, and actionability are the hard gates."
            ),
        ],
    }


def build_watch_metrics(
    summary: dict[str, Any],
    *,
    metric_profile: str,
    judge_status: str = "ready",
) -> list[dict[str, Any]]:
    """Return reported metrics that are not hard gates in the current profile."""
    thresholds = ragas_thresholds()
    if metric_profile == "full" and judge_status != "ready":
        reason = str(summary.get("not_run_reason") or summary.get("judge_failure_reason") or "")
        return [
            watch_metric(
                metric,
                label,
                None,
                threshold,
                source,
                f"Judge metric {judge_status}: {reason}",
            )
            for metric, label, threshold, source in [
                (
                    "faithfulness",
                    "Faithfulness",
                    thresholds["faithfulness"],
                    "summary.faithfulness_avg",
                ),
                (
                    "response_relevancy",
                    "Response relevancy",
                    thresholds["response_relevancy"],
                    "summary.response_relevancy_avg",
                ),
                (
                    "judge_oncall_actionability",
                    "Judge OnCall actionability",
                    thresholds["oncall_actionability"],
                    "summary.judge_oncall_actionability_avg",
                ),
                (
                    "answer_completeness",
                    "Answer completeness",
                    1.0,
                    "summary.answer_completeness_avg",
                ),
            ]
        ]
    if metric_profile != "id-smoke":
        return []
    return [
        watch_metric(
            "id_context_precision",
            "Trusted-source precision",
            summary.get("id_context_precision_avg"),
            thresholds["id_context_precision"],
            "summary.id_context_precision_avg",
            "Observed in id-smoke to reveal Top-K noise, but not a hard failure.",
        ),
        watch_metric(
            "faithfulness",
            "RAGAS faithfulness",
            None,
            thresholds["faithfulness"],
            "summary.faithfulness_avg",
            "Not run in id-smoke; use --metrics-profile full with a judge key.",
            status="not_run",
        ),
        watch_metric(
            "response_relevancy",
            "RAGAS response relevancy",
            None,
            thresholds["response_relevancy"],
            "summary.response_relevancy_avg",
            "Not run in id-smoke; use --metrics-profile full with a judge key.",
            status="not_run",
        ),
    ]


def build_quality_risk_register(
    results: list[dict[str, Any]],
    *,
    watch_metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build reviewable risks from failed cases and watch-only signals."""
    risks = [
        {
            "key": f"failed_case:{result.get('id', 'unknown')}",
            "severity": "high",
            "status": "open",
            "reason": ", ".join(result.get("failed_metrics", [])) or "quality gate failed",
            "next_step": result.get("suggested_backlog_category") or "inspect_failed_case",
        }
        for result in results
        if not result.get("passed")
    ]
    risks.extend(
        {
            "key": f"watch:{metric['key']}",
            "severity": "medium" if metric["status"] == "watch" else "info",
            "status": metric["status"],
            "reason": metric["note"],
            "next_step": "promote to a hard gate only after full-profile evidence is stable",
        }
        for metric in watch_metrics
        if metric.get("status") in {"watch", "not_run"}
    )
    if not risks:
        risks.append(
            {
                "key": "quality_contract:clean",
                "severity": "info",
                "status": "closed",
                "reason": "No failed hard gate in the current fixed-case RAGAS suite.",
                "next_step": "add new production bad cases before relaxing thresholds",
            }
        )
    return risks


def quality_gate(
    key: str,
    label: str,
    value: Any,
    threshold: float,
    source: str,
    reason: str,
    *,
    applicable: bool = True,
) -> dict[str, Any]:
    """Build one hard quality gate row."""
    numeric = safe_float(value)
    status = "not_applicable" if not applicable else "passed" if numeric >= threshold else "failed"
    return {
        "key": key,
        "label": label,
        "value": numeric if applicable else None,
        "threshold": threshold,
        "status": status,
        "source": source,
        "business_reason": reason,
    }


def watch_metric(
    key: str,
    label: str,
    value: Any,
    threshold: float,
    source: str,
    note: str,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    """Build one watch-only metric row."""
    numeric = safe_float(value) if value is not None else None
    final_status = status or ("passed" if numeric is not None and numeric >= threshold else "watch")
    return {
        "key": key,
        "label": label,
        "value": numeric,
        "threshold": threshold,
        "status": final_status,
        "source": source,
        "note": note,
    }


def render_summary(payload: dict[str, Any]) -> str:
    """Render a compact CLI summary."""
    run = payload["run"]
    summary = payload["summary"]
    contract = payload.get("quality_contract", {})
    if summary.get("status") == "not_run":
        return (
            "RAGAS eval: status=not_run "
            f"profile={run.get('metric_profile', 'unknown')} "
            f"deterministic={summary.get('deterministic_status', 'unknown')} "
            f"cases={summary.get('passed_count', 0)}/{summary.get('case_count', 0)} "
            f"reason={summary.get('not_run_reason', 'judge unavailable')}"
        )
    lines = [
        (
            f"RAGAS eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"profile={run.get('metric_profile', 'unknown')} "
            f"status={summary['status']} "
            f"contract={contract.get('status', 'unknown')} "
            f"faith={format_contract_value(summary['faithfulness_avg'])} "
            f"relevancy={format_contract_value(summary['response_relevancy_avg'])} "
            f"id_precision={summary['id_context_precision_avg']:.2f} "
            f"id_recall={summary['id_context_recall_avg']:.2f} "
            f"actionability={summary['oncall_actionability_avg']:.2f} "
            f"refusal={summary['refusal_boundary_rate']:.0%}"
        )
    ]
    for result in payload["case_scores"]:
        status = "PASS" if result["passed"] else "FAIL"
        suffix = "" if result["passed"] else f" failed={','.join(result['failed_metrics'])}"
        lines.append(f"- {status} {result['id']} policy={result['answer_policy']}{suffix}")
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    """Render a reproducible Markdown quality report."""
    run = payload["run"]
    summary = payload["summary"]
    contract = payload.get("quality_contract", {})
    judge_text = (
        "not_required_for_id_smoke"
        if run.get("metric_profile") == "id-smoke"
        else str(run.get("judge_model", ""))
    )
    embedding_text = (
        "not_required_for_id_smoke"
        if run.get("metric_profile") == "id-smoke"
        else str(run.get("embedding_model", ""))
    )
    if summary.get("status") == "not_run":
        return "\n".join(
            [
                "# AutoOnCall RAGAS Quality Summary",
                "",
                "## Run",
                f"- Generated at: `{run.get('ended_at', '')}`",
                f"- Metric profile: `{run.get('metric_profile', '')}`",
                "- Status: `not_run`",
                f"- Reason: {summary.get('not_run_reason', '')}",
                f"- Selected cases: `{summary.get('case_count', 0)}`",
                f"- Repeat count: `{run.get('repeat_count', 1)}`",
                f"- Deterministic status: `{summary.get('deterministic_status', '')}`",
                (
                    "- Deterministic cases: "
                    f"`{summary.get('passed_count', 0)}/{summary.get('case_count', 0)}`"
                ),
                f"- Human review status: `{summary.get('human_review', {}).get('status', '')}`",
                "",
                (
                    "> Deterministic rules still ran. Missing judge credentials never become "
                    "a zero score or a false pass."
                ),
                "",
            ]
        )
    lines = [
        "# AutoOnCall RAGAS Quality Summary",
        "",
        "## Run",
        f"- Generated at: `{run.get('ended_at', '')}`",
        f"- Mode: `{run.get('mode', '')}`",
        f"- Answer source: `{run.get('answer_source', '')}`",
        f"- Metric profile: `{run.get('metric_profile', '')}`",
        f"- Repeat count: `{run.get('repeat_count', 1)}`",
        f"- Judge model: `{judge_text}`",
        f"- Embedding model: `{embedding_text}`",
        f"- RAGAS version: `{run.get('ragas_version', '')}`",
        f"- Scope: {run.get('evaluation_scope', '')}",
        *provenance_markdown_lines(run.get("environment", {})),
        "",
        "## Metrics",
        f"- Status: `{summary['status']}`",
        f"- Cases: `{summary['passed_count']}/{summary['case_count']}`",
        f"- Core case pass rate: `{summary['core_case_pass_rate']:.0%}`",
        f"- Faithfulness avg: `{format_contract_value(summary['faithfulness_avg'])}`",
        (f"- Response relevancy avg: `{format_contract_value(summary['response_relevancy_avg'])}`"),
        (
            "- Judge OnCall actionability avg: "
            f"`{format_contract_value(summary.get('judge_oncall_actionability_avg'))}`"
        ),
        (
            "- Answer completeness avg: "
            f"`{format_contract_value(summary.get('answer_completeness_avg'))}`"
        ),
        f"- ID context precision avg: `{summary['id_context_precision_avg']:.2f}`",
        f"- ID context recall avg: `{summary['id_context_recall_avg']:.2f}`",
        f"- OnCall actionability avg: `{summary['oncall_actionability_avg']:.2f}`",
        f"- Citation existence rate: `{summary.get('citation_existence_rate', 0.0):.0%}`",
        f"- Citation support rate: `{summary.get('citation_support_rate', 0.0):.0%}`",
        f"- Citation correctness rate: `{summary.get('citation_correctness_rate', 0.0):.0%}`",
        f"- Factual error rate: `{summary.get('factual_error_rate', 0.0):.0%}`",
        f"- Severe hallucination rate: `{summary.get('severe_hallucination_rate', 0.0):.0%}`",
        f"- Refusal boundary rate: `{summary['refusal_boundary_rate']:.0%}`",
        f"- All repeats pass: `{summary.get('stability', {}).get('all_cases_all_pass', False)}`",
        (
            "- Human/automatic agreement: "
            f"`{format_contract_value(summary.get('human_review', {}).get('agreement_rate'))}`"
        ),
        "",
        "> RAGAS results are fixed-case quality regressions, not online accuracy claims.",
        "",
        "## AutoOnCall Quality Contract",
        f"- Contract status: `{contract.get('status', 'unknown')}`",
        f"- Case mix: `{contract.get('case_mix', {})}`",
        "",
        "### Hard Gates",
        "| Gate | Value | Threshold | Status | Why it matters |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in contract.get("hard_gates", []) if isinstance(contract, dict) else []:
        lines.append(
            "| "
            f"{gate.get('label', gate.get('key', ''))} | "
            f"{format_contract_value(gate.get('value'))} | "
            f"{format_contract_value(gate.get('threshold'))} | "
            f"{gate.get('status', '')} | "
            f"{gate.get('business_reason', '')} |"
        )
    watch_metrics = contract.get("watch_metrics", []) if isinstance(contract, dict) else []
    if watch_metrics:
        lines.extend(
            [
                "",
                "### Watch Metrics",
                "| Metric | Value | Threshold | Status | Note |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for metric in watch_metrics:
            lines.append(
                "| "
                f"{metric.get('label', metric.get('key', ''))} | "
                f"{format_contract_value(metric.get('value'))} | "
                f"{format_contract_value(metric.get('threshold'))} | "
                f"{metric.get('status', '')} | "
                f"{metric.get('note', '')} |"
            )
    talk_track = contract.get("interview_talk_track", []) if isinstance(contract, dict) else []
    if talk_track:
        lines.extend(["", "### Interview Talk Track"])
        lines.extend(f"- {item}" for item in talk_track)
    lines.extend(
        [
            "",
            "## Failed Cases",
        ]
    )
    if summary["failed_cases"]:
        for item in summary["failed_cases"]:
            lines.append(
                f"- [{item.get('suite', 'ragas')}] {item['id']}: "
                f"{', '.join(item['failed_metrics'])}; "
                f"{'; '.join(item['failure_reasons'].values())}"
            )
    else:
        lines.append("- None.")

    lines.extend(
        [
            "",
            "## Case Scores",
            "| Case | Core | Result | Policy | Failed metrics |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for result in payload["case_scores"]:
        lines.append(
            "| "
            f"{result['id']} | "
            f"{'yes' if result.get('core_case') else 'no'} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | "
            f"{result.get('answer_policy', '')} | "
            f"{', '.join(result.get('failed_metrics', [])) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def format_contract_value(value: Any) -> str:
    """Format values in the Markdown contract section."""
    if value is None:
        return "not_run"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def write_eval_artifacts(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
    failed_cases_path: str | Path | None = None,
) -> dict[str, str]:
    """Write JSON and Markdown RAGAS summaries."""
    written: dict[str, str] = {}
    if summary_json_path:
        written["summary_json"] = str(Path(summary_json_path))
    if summary_md_path:
        written["summary_md"] = str(Path(summary_md_path))
    if failed_cases_path:
        written["failed_cases_json"] = str(Path(failed_cases_path))
    payload["run"]["artifacts"] = written
    if summary_json_path:
        path = Path(summary_json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(json_safe_payload(payload), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    if summary_md_path:
        path = Path(summary_md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown_summary(payload), encoding="utf-8")
    if failed_cases_path:
        path = Path(failed_cases_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                json_safe_payload(
                    {
                        "schema_version": 1,
                        "run": payload.get("run", {}),
                        "thresholds": payload.get("thresholds", {}),
                        "failed_cases": payload.get("summary", {}).get("failed_cases", []),
                    }
                ),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
    return written


def build_runner_context(
    *,
    mode: str,
    answer_source: str,
    top_k: int,
    min_score: float,
    metric_profile: str,
) -> dict[str, Any]:
    """Return stable metadata used by the metric runner."""
    return {
        "mode": mode,
        "answer_source": answer_source,
        "answer_generation_mode": answer_generation_mode(answer_source),
        "retrieval_evidence_mode": retrieval_evidence_mode(mode),
        "top_k": top_k,
        "min_score": min_score,
        "metric_profile": metric_profile,
        "judge_model": config.effective_ragas_eval_model,
        "embedding_model": config.effective_ragas_eval_embedding_model,
        "api_base": config.dashscope_api_base,
        "temperature": 0,
        "judge_diagnostics": [],
    }


def answer_generation_mode(answer_source: str) -> str:
    """Describe whether a run evaluates fixture text or actual grounded generation."""
    modes = {
        "product-offline": "reference_fixture_via_product_contract",
        "reference-fixture": "reference_fixture_direct",
        "context-fixture": "real_grounded_llm",
        "runtime": "real_grounded_llm",
    }
    return modes.get(answer_source, "unknown")


def retrieval_evidence_mode(mode: str) -> str:
    """Describe whether retrieval is deterministic offline data or the runtime stack."""
    return "fixed_offline_retrieval" if mode == "offline" else "runtime_milvus_retrieval"


def case_set_sha256(cases: list[dict[str, Any]]) -> str:
    """Fingerprint the frozen case content, not only its file path."""
    canonical = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def json_safe_payload(value: Any) -> Any:
    """Replace non-finite values before writing portable JSON artifacts."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe_payload(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe_payload(item) for item in value]
    return value


def judge_metric_diagnostic(
    *,
    case_id: str,
    repeat_index: int,
    metric: str,
    raw_value: Any,
    value: float | None,
    duration_ms: float,
    error: str = "",
) -> dict[str, Any]:
    """Persist enough Judge state to diagnose NaN, field, timeout, and API failures."""
    raw_type = type(raw_value).__name__ if raw_value is not None else "NoneType"
    return {
        "case_id": case_id,
        "repeat_index": repeat_index,
        "metric": metric,
        "status": "available" if value is not None else "unavailable",
        "raw_value": str(raw_value) if raw_value is not None else None,
        "raw_value_type": raw_type,
        "value": value,
        "is_finite": value is not None,
        "duration_ms": duration_ms,
        "error": error,
    }


def judge_diagnostics_for_case(
    runner_context: dict[str, Any],
    case_id: str,
    *,
    repeat_index: int,
) -> list[dict[str, Any]]:
    """Return this case's per-metric Judge attempts for its repeat."""
    diagnostics = runner_context.get("judge_diagnostics", [])
    return [
        item
        for item in diagnostics
        if item.get("case_id") == case_id and item.get("repeat_index") == repeat_index
    ]


def contexts_from_retrieval(payload: dict[str, Any]) -> list[str]:
    """Extract full text contexts from retrieval results."""
    contexts = []
    for item in payload.get("retrieval_results", []) or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        if content:
            contexts.append(content)
    return contexts


def contexts_for_citations(
    retrieval_payload: dict[str, Any],
    citations: list[dict[str, Any]],
) -> list[str]:
    """Return semantic Judge contexts in the same order as answer citations."""
    if not citations:
        return contexts_from_retrieval(retrieval_payload)

    results = [
        item
        for item in retrieval_payload.get("retrieval_results", []) or []
        if isinstance(item, dict)
    ]
    result_by_chunk = {
        str(item.get("chunk_id") or "").strip(): item
        for item in results
        if str(item.get("chunk_id") or "").strip()
    }
    contexts: list[str] = []
    seen_chunks: set[str] = set()
    for citation in citations:
        chunk_id = str(citation.get("chunk_id") or "").strip()
        if not chunk_id or chunk_id in seen_chunks:
            continue
        item = result_by_chunk.get(chunk_id)
        if item is None:
            continue
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        if content:
            contexts.append(content)
            seen_chunks.add(chunk_id)
    return contexts or contexts_from_retrieval(retrieval_payload)


def context_ids_from_retrieval(payload: dict[str, Any]) -> list[str]:
    """Extract stable file-level ids for ID-based RAGAS metrics.

    The existing eval cases mostly define expected sources at source-file granularity.
    Keeping RAGAS ID metrics at the same granularity avoids fake misses such as
    ``cpu.md#cpu.md#0001`` versus ``cpu.md``.
    """
    ids = []
    for item in payload.get("retrieval_results", []) or []:
        if not isinstance(item, dict):
            continue
        source_file = context_id_from_chunk(item)
        if source_file and source_file not in ids:
            ids.append(source_file)
    return ids


def context_id_from_chunk(item: dict[str, Any]) -> str:
    """Extract a file-level context id from one retrieval chunk."""
    for key in ("source_file", "source_path"):
        normalized = normalize_context_id(item.get(key))
        if normalized:
            return Path(normalized).name
    chunk_id = normalize_context_id(item.get("chunk_id"))
    return Path(chunk_id).name if chunk_id else ""


def reference_context_ids(case: dict[str, Any]) -> list[str]:
    """Return reference context ids from explicit case metadata or expected sources."""
    explicit = case.get("reference_context_ids")
    if isinstance(explicit, list) and explicit:
        return _unique_context_ids(explicit)
    return expected_sources(case)


def expected_sources(case: dict[str, Any]) -> list[str]:
    """Return expected source files from the existing RAG eval schema."""
    sources = case.get("expected_sources")
    if isinstance(sources, list) and sources:
        return _unique_context_ids(sources)
    source = case.get("expected_source")
    normalized = normalize_context_id(source)
    return [normalized] if normalized else []


def normalize_context_id(value: Any) -> str:
    """Normalize source or source#chunk ids to source-file granularity."""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split("#", 1)[0].strip()


def _unique_context_ids(values: list[Any]) -> list[str]:
    ids: list[str] = []
    for value in values:
        normalized = normalize_context_id(value)
        if normalized and normalized not in ids:
            ids.append(normalized)
    return ids


def format_offline_context(retrieved: list[dict[str, Any]]) -> str:
    """Format offline chunks in the same spirit as runtime retrieval context."""
    lines = []
    for index, item in enumerate(retrieved, 1):
        lines.append(
            "\n".join(
                [
                    f"[{index}] source_file: {item.get('source_file', '')}",
                    f"chunk_id: {item.get('chunk_id', '')}",
                    f"score: {item.get('offline_score', item.get('score', ''))}",
                    str(item.get("content") or item.get("content_preview") or ""),
                ]
            )
        )
    return "\n\n".join(lines)


def rubrics_from_case(case: dict[str, Any]) -> dict[str, str]:
    """Convert business rubric list into a RAGAS-compatible rubric map."""
    rubric = case.get("business_rubric")
    if not isinstance(rubric, list):
        return {}
    return {
        f"business_requirement_{index}": str(item)
        for index, item in enumerate(rubric, 1)
        if str(item).strip()
    }


def business_metric_scores(sample: RagasCaseSample) -> dict[str, float]:
    """Compute deterministic business-aware guard metrics."""
    answer = sample.answer.lower()
    rubric_items = [str(item).lower() for item in sample.case.get("business_rubric", []) or []]
    actionability_hits = sum(1 for item in rubric_items if business_requirement_hit(item, answer))
    actionability = ratio(actionability_hits, len(rubric_items)) if rubric_items else 1.0
    domain_hit = business_domain_hit(sample)
    evidence_hit = business_evidence_hit(answer)
    operation_hit = business_operation_hit(answer)
    if rubric_items:
        actionability = min(
            actionability,
            ratio(sum([domain_hit, evidence_hit, operation_hit]), 3),
        )
    citation_quality = citation_quality_scores(sample)
    incident_boundary = 1.0
    if any(term in answer for term in ["redis", "mysql", "incident-window", "runbook"]):
        incident_boundary = 1.0 if boundary_language_hit(answer) else 0.0
    confusion = 1.0
    if sample.case.get("case_type") == "confusion":
        confusion = 1.0 if confusion_target_hit(sample) else 0.0
    return {
        "oncall_actionability_score": round(actionability, 4),
        "business_domain_hit": 1.0 if domain_hit else 0.0,
        "business_evidence_hit": 1.0 if evidence_hit else 0.0,
        "business_operation_hit": 1.0 if operation_hit else 0.0,
        **citation_quality,
        "factual_error_hit": 1.0 if deterministic_factual_error(sample) else 0.0,
        "severe_hallucination_hit": 1.0 if deterministic_severe_hallucination(sample) else 0.0,
        "incident_boundary_hit": incident_boundary,
        "confusion_disambiguation_hit": confusion,
    }


def citation_quality_scores(sample: RagasCaseSample) -> dict[str, float]:
    """Measure citation existence, retrieved-context support, and expected-source correctness."""
    valid = [
        item
        for item in sample.citations
        if str(item.get("source_file") or "").strip() and str(item.get("chunk_id") or "").strip()
    ]
    existence = 1.0 if valid and has_answer_citation(sample) else 0.0
    retrieved_ids = set(sample.retrieved_context_ids)
    expected_ids = set(sample.reference_context_ids)
    cited_ids = {
        normalize_context_id(item.get("source_file") or item.get("chunk_id")) for item in valid
    }
    cited_ids.discard("")
    support = ratio(len(cited_ids & retrieved_ids), len(cited_ids)) if cited_ids else 0.0
    correctness = ratio(len(cited_ids & expected_ids), len(cited_ids)) if cited_ids else 0.0
    return {
        "citation_grounding_hit": existence,
        "citation_existence_hit": existence,
        "citation_support_score": support,
        "citation_correctness_score": correctness,
    }


def deterministic_factual_error(sample: RagasCaseSample) -> bool:
    """Flag explicit expected/forbidden fact violations without pretending to be a judge."""
    answer = sample.answer.lower()
    required = [
        str(item).strip().lower()
        for item in sample.case.get("required_facts", []) or []
        if str(item).strip()
    ]
    forbidden = [
        str(item).strip().lower()
        for item in sample.case.get("forbidden_facts", []) or []
        if str(item).strip()
    ]
    return any(item not in answer for item in required) or any(item in answer for item in forbidden)


def deterministic_severe_hallucination(sample: RagasCaseSample) -> bool:
    """Flag severe deterministic failures: unsupported remediation or false live evidence."""
    if is_refusal_case(sample.case):
        return hallucinated_remediation(sample.answer)
    answer = sample.answer.lower()
    unsupported_live_claim = any(
        marker in answer
        for marker in [
            "currently at",
            "current value is",
            "live metric shows",
            "实时指标显示",
            "当前值为",
        ]
    ) and not any(marker in answer for marker in ["incident-window", "live evidence", "实时证据"])
    unsupported_action = any(
        marker in answer
        for marker in ["kubectl delete", "drop table", "flushall", "直接重启", "立即删除"]
    ) and not any(marker in answer for marker in ["approval", "dry-run", "审批"])
    return unsupported_live_claim or unsupported_action


def business_token_overlap(requirement: str, answer: str) -> bool:
    """Return true when rubric domain terms or CJK ngrams appear in the answer."""
    return any(token in answer for token in extract_business_tokens(requirement))


def business_requirement_hit(requirement: str, answer: str) -> bool:
    """Match rubric intent conservatively across equivalent OnCall wording."""
    if business_token_overlap(requirement, answer):
        return True
    groups = (
        (
            {"区分", "诊断证据", "处置动作", "不直接执行"},
            {"evidence", "incident-window", "check", "confirm"},
            {"approval", "dry-run", "rollback", "审批", "回滚"},
        ),
        (
            {"审批", "回滚边界", "安全变更", "不越过", "不自动执行"},
            {"approval", "dry-run", "rollback", "审批", "回滚", "安全变更"},
        ),
        (
            {"告警", "用户可见", "症状"},
            {"alert", "symptom", "user", "visible", "告警", "症状", "用户可见"},
        ),
        (
            {"历史", "当前事件", "实时", "incident-window"},
            {"history", "incident-window", "历史", "当前", "实时", "evidence"},
        ),
        (
            {"相关性", "根因", "直接当作"},
            {"结合", "判断", "evidence", "current impact", "当前影响"},
        ),
    )
    for requirement_terms, *answer_term_groups in groups:
        if any(term in requirement for term in requirement_terms) and all(
            any(term in answer for term in answer_terms) for answer_terms in answer_term_groups
        ):
            return True
    return False


def business_domain_hit(sample: RagasCaseSample) -> bool:
    """Require the answer to mention the expected incident domain or source."""
    answer = sample.answer.lower()
    expected = " ".join(expected_sources(sample.case)).lower()
    query = str(sample.case.get("query") or "").lower()
    source_terms = {
        "cpu": ["cpu"],
        "memory": ["memory", "oom", "jvm", "gc", "内存"],
        "disk": ["disk", "docker", "inode", "磁盘"],
        "slow_response": ["slow", "sql", "mysql", "pool", "慢", "连接池"],
        "service_unavailable": ["503", "5xx", "redis", "mq", "timeout", "依赖", "超时"],
        "redis": ["redis", "maxclients", "connected_clients"],
        "mysql": ["mysql", "sql", "active_connections", "pool_waiting", "explain"],
        "payment": ["payment", "mysql", "pool_waiting", "explain"],
    }
    candidate_terms: list[str] = []
    for source_marker, terms in source_terms.items():
        if source_marker in expected or source_marker in query:
            candidate_terms.extend(terms)
    if not candidate_terms:
        candidate_terms.extend(extract_business_tokens(" ".join(expected_sources(sample.case))))
    return bool(candidate_terms) and any(term in answer for term in candidate_terms)


def business_evidence_hit(answer: str) -> bool:
    """Require evidence-oriented language, not only generic remediation."""
    return any(
        marker in answer
        for marker in [
            "evidence",
            "metric",
            "log",
            "trace",
            "source_file",
            "chunk_id",
            "incident-window",
            "runbook",
            "证据",
            "指标",
            "日志",
            "知识库",
            "复盘",
        ]
    )


def business_operation_hit(answer: str) -> bool:
    """Require an OnCall action or decision boundary."""
    return any(
        marker in answer
        for marker in [
            "check",
            "confirm",
            "rollback",
            "approval",
            "dry-run",
            "limit",
            "scale",
            "degrade",
            "restart",
            "runbook",
            "排查",
            "确认",
            "回滚",
            "审批",
            "检查",
            "建议检查",
            "限流",
            "扩容",
            "降级",
            "重启",
        ]
    )


def extract_business_tokens(text: str) -> list[str]:
    """Extract useful Chinese and ASCII scoring tokens from a rubric item."""
    import re

    lowered = text.lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    tokens.update("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    tokens.update("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    domain_terms = {
        "redis",
        "mysql",
        "sql",
        "cpu",
        "mq",
        "p95",
        "maxclients",
        "connected_clients",
        "explain",
        "pool_waiting",
        "active_connections",
        "慢查询",
        "连接池",
        "依赖",
        "超时",
        "审批",
        "回滚",
        "限流",
        "扩容",
        "证据",
        "风险",
        "主故障域",
    }
    tokens.update(term for term in domain_terms if term in lowered)
    return sorted(token for token in tokens if len(token.strip()) >= 2)


def any_token_overlap(requirement: str, answer: str) -> bool:
    """Return true when meaningful rubric tokens appear in the answer."""
    tokens = [
        token
        for token in requirement.replace("/", " ").replace("、", " ").split()
        if len(token.strip()) >= 2
    ]
    return any(token in answer for token in tokens)


def has_answer_citation(sample: RagasCaseSample) -> bool:
    """Check whether the generated answer contains a source_file + chunk_id citation."""
    for item in sample.citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if source_file and chunk_id and source_file in sample.answer and chunk_id in sample.answer:
            return True
    return False


def boundary_language_hit(answer: str) -> bool:
    """Require language that separates runbook knowledge from live evidence."""
    return any(
        marker in answer
        for marker in [
            "runbook",
            "知识库",
            "复盘",
            "incident-window",
            "实时",
            "当前",
            "证据",
        ]
    )


def confusion_target_hit(sample: RagasCaseSample) -> bool:
    """Check that confusion cases mention the expected source domain."""
    expected = " ".join(expected_sources(sample.case)).lower()
    answer = sample.answer.lower()
    if "slow_response" in expected:
        return any(term in answer for term in ["慢", "sql", "mysql", "连接池", "数据库"])
    if "service_unavailable" in expected:
        return any(term in answer for term in ["依赖", "redis", "mq", "timeout", "超时", "503"])
    if "cpu" in expected:
        return "cpu" in answer
    return True


def failed_quality_metrics(
    metrics: dict[str, Any],
    sample: RagasCaseSample,
    *,
    metric_profile: str,
    judge_metrics_available: bool = True,
) -> list[str]:
    """Return failed metrics using configured thresholds."""
    thresholds = ragas_thresholds()
    failed = []
    checks = {}
    if metric_profile == "full" and judge_metrics_available:
        checks.update(
            {
                "faithfulness": thresholds["faithfulness"],
                "answer_relevancy": thresholds["response_relevancy"],
                "judge_oncall_actionability": thresholds["oncall_actionability"],
                "answer_completeness": 1.0,
            }
        )
    checks.update(
        {
            "id_based_context_recall": thresholds["id_context_recall"],
            "oncall_actionability_score": thresholds["oncall_actionability"],
            "citation_grounding_hit": 1.0,
            "incident_boundary_hit": 1.0,
            "confusion_disambiguation_hit": 1.0,
        }
    )
    if metric_profile == "full":
        checks["id_based_context_precision"] = thresholds["id_context_precision"]
    for metric, threshold in checks.items():
        value = metrics.get(metric)
        if value is None:
            continue
        if value < threshold:
            failed.append(metric)
    if metrics.get("factual_error_hit", 0.0) > 0:
        failed.append("factual_error")
    if metrics.get("severe_hallucination_hit", 0.0) > 0:
        failed.append("severe_hallucination")
    if metric_profile == "full" and judge_metrics_available and is_core_case(sample.case):
        for metric in [
            "faithfulness",
            "answer_relevancy",
            "judge_oncall_actionability",
            "answer_completeness",
            "oncall_actionability_score",
        ]:
            value = metrics.get(metric)
            if value is not None and metric not in failed and value < checks[metric]:
                failed.append(metric)
    return failed


def quality_failure_reasons(failed_metrics: list[str]) -> dict[str, str]:
    """Map failed RAGAS and business metrics to actionable reasons."""
    reasons = {
        "faithfulness": "Answer is not sufficiently supported by retrieved Runbook context.",
        "answer_relevancy": "Answer does not stay focused on the user's OnCall question.",
        "judge_oncall_actionability": "Judge found the OnCall actions insufficiently concrete or bounded.",
        "answer_completeness": "Judge found missing evidence, citations, actions, or safety boundaries.",
        "faithfulness_unavailable": "Judge faithfulness metric did not return a finite score.",
        "answer_relevancy_unavailable": (
            "Judge answer relevancy metric did not return a finite score."
        ),
        "judge_oncall_actionability_unavailable": (
            "Judge OnCall actionability metric did not return a finite score."
        ),
        "answer_completeness_unavailable": (
            "Judge answer completeness metric did not return a finite score."
        ),
        "id_based_context_precision": "Retrieved context ids include too many non-reference chunks.",
        "id_based_context_recall": "Retrieved context ids miss expected reference chunks.",
        "oncall_actionability_score": "Answer misses required OnCall actionability rubric items.",
        "citation_grounding_hit": "Answer lacks auditable source_file + chunk_id citations.",
        "citation_support_score": "One or more citations are not present in retrieved context.",
        "citation_correctness_score": "One or more citations do not match expected sources.",
        "factual_error": "Answer violates deterministic required/forbidden fact checks.",
        "severe_hallucination": "Answer makes an unsupported live claim or unsafe action claim.",
        "incident_boundary_hit": "Answer does not separate Runbook knowledge from live incident evidence.",
        "confusion_disambiguation_hit": "Answer does not select the expected primary fault domain.",
    }
    return {metric: reasons.get(metric, metric) for metric in failed_metrics}


def refusal_boundary_hit(sample: RagasCaseSample) -> bool:
    """Return whether an out-of-scope case is refused by final RAG behavior."""
    return (
        sample.no_answer
        and sample.answer_policy in {"refuse_without_trusted_source", "refuse_without_citation"}
        and not sample.citations
        and explicit_refusal_boundary_language(sample.answer)
        and not hallucinated_remediation(sample.answer)
    )


def explicit_refusal_boundary_language(answer: str) -> bool:
    """Require an auditable refusal reason, not a silent empty answer."""
    lowered = answer.lower()
    return any(
        marker in lowered
        for marker in [
            "no trusted source",
            "trusted knowledge",
            "knowledge base",
            "out of scope",
            "cannot answer",
            "add documentation",
            "补充",
            "知识库",
            "可信",
            "范围外",
            "无法回答",
            "鏈壘鍒",
            "鍙俊",
            "鐭ヨ瘑搴",
            "琛ュ厖",
            "鏃犳硶",
        ]
    )


def hallucinated_remediation(answer: str) -> bool:
    """Catch obvious remediation advice in a refusal answer."""
    lowered = answer.lower()
    return any(term in lowered for term in ["kubectl", "sql", "扩容", "重启", "rollback", "回滚"])


def suggested_backlog_category(failed_metrics: list[str]) -> str:
    """Map failed metrics into existing feedback backlog categories."""
    failed = set(failed_metrics)
    if failed & {"id_based_context_precision", "id_based_context_recall"}:
        return "retrieval_failure"
    if "citation_grounding_hit" in failed:
        return "missing_citation"
    return "hallucination_risk" if failed else ""


def retrieved_sources(sample: RagasCaseSample) -> list[str]:
    """Return unique retrieved sources for reports."""
    return sorted({item.split("#", 1)[0] for item in sample.retrieved_context_ids if item})


def is_refusal_case(case: dict[str, Any]) -> bool:
    """Return true for cases expected to be refused."""
    return bool(case.get("should_reject")) or REFUSAL_TAG in ragas_tags(case)


def is_core_case(case: dict[str, Any]) -> bool:
    """Return true for interview-critical RAGAS cases."""
    return CORE_TAG in ragas_tags(case)


def ragas_tags(case: dict[str, Any]) -> list[str]:
    """Return normalized RAGAS tags from a case."""
    tags = case.get("ragas_tags")
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def ragas_thresholds() -> dict[str, float]:
    """Return threshold config included in each artifact."""
    return {
        "faithfulness": float(config.ragas_min_faithfulness),
        "response_relevancy": float(config.ragas_min_response_relevancy),
        "id_context_precision": float(config.ragas_min_id_context_precision),
        "id_context_recall": float(config.ragas_min_id_context_recall),
        "oncall_actionability": float(config.ragas_min_oncall_actionability),
        "refusal_boundary_rate": 1.0,
        "core_case_pass_rate": 1.0,
    }


def average_metric(results: list[dict[str, Any]], metric: str) -> float:
    """Return average metric score."""
    values = [safe_float(result.get("metrics", {}).get(metric, 0.0)) for result in results]
    return round(sum(values) / len(values), 4) if values else 0.0


def average_optional_metric(results: list[dict[str, Any]], metric: str) -> float | None:
    """Average a metric only when it was actually executed."""
    values = [
        safe_float(value)
        for result in results
        if (value := result.get("metrics", {}).get(metric)) is not None
    ]
    return round(sum(values) / len(values), 4) if values else None


def ratio(numerator: int | float, denominator: int | float) -> float:
    """Return a rounded ratio with stable zero-denominator behavior."""
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def safe_float(value: Any) -> float:
    """Convert RAGAS metric values into JSON-safe floats."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return round(numeric, 4)


def optional_metric_float(value: Any) -> float | None:
    """Return a finite metric value without turning evaluator failures into zero scores."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, 4)


def safe_package_version(name: str) -> str:
    """Return installed package version or a stable unavailable marker."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not_installed"


def _select_cases(cases: list[dict[str, Any]], *, max_cases: int | None) -> list[dict[str, Any]]:
    if max_cases is None or max_cases <= 0:
        return cases
    return cases[:max_cases]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--mode", choices=sorted(SUPPORTED_MODES), default="offline")
    parser.add_argument(
        "--answer-source",
        choices=sorted(SUPPORTED_ANSWER_SOURCES),
        default=DEFAULT_ANSWER_SOURCE,
    )
    parser.add_argument(
        "--metrics-profile",
        choices=sorted(SUPPORTED_METRIC_PROFILES),
        default=DEFAULT_METRIC_PROFILE,
        help=(
            "id-smoke runs RAGAS ID metrics and deterministic business gates without a judge key; "
            "full also runs LLM-as-judge faithfulness and relevancy."
        ),
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=1,
        help="Repeat every selected case; use 3 for the interview answer-quality run.",
    )
    parser.add_argument(
        "--human-review",
        default=str(DEFAULT_HUMAN_REVIEW_PATH),
        help="Optional JSON rubric review file compared with automatic decisions.",
    )
    parser.add_argument(
        "--export-human-review-template",
        help=(
            "Write a blind 0-2 rubric template after evaluation. "
            "Use eval/ragas_cases.review.json for the formal review artifact."
        ),
    )
    parser.add_argument("--reviewer", default="", help="Reviewer id stored in exported items.")
    parser.add_argument(
        "--human-review-items",
        type=int,
        default=30,
        help="Maximum answer runs exported for blind human review.",
    )
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
    parser.add_argument(
        "--failed-cases-json",
        help=(
            "Optional focused failure artifact. Use this for the full frozen-core profile "
            "so each failed answer keeps contexts, citations, and Judge diagnostics."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    try:
        payload = asyncio.run(
            evaluate_cases(
                args.cases,
                docs_dir=args.docs_dir,
                mode=args.mode,
                answer_source=args.answer_source,
                top_k=args.top_k,
                min_score=args.min_score,
                max_cases=args.max_cases,
                metric_profile=args.metrics_profile,
                repeat_count=args.repeat_count,
                human_review_path=args.human_review,
            )
        )
    except Exception as exc:
        payload = build_failed_payload(args, exc)
    written = write_eval_artifacts(
        payload,
        summary_json_path=args.summary_json,
        summary_md_path=args.summary_md,
        failed_cases_path=args.failed_cases_json,
    )
    if args.export_human_review_template:
        written["human_review_template"] = write_human_review_template(
            payload,
            args.export_human_review_template,
            reviewer=args.reviewer,
            max_items=args.human_review_items,
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_summary(payload))
        if written:
            print("Artifacts: " + ", ".join(f"{key}={value}" for key, value in written.items()))
    summary = payload["summary"]
    success = summary["status"] == "passed" or (
        summary["status"] == "not_run" and summary.get("deterministic_status") == "passed"
    )
    return 0 if success else 1


def build_failed_payload(args: argparse.Namespace, exc: Exception) -> dict[str, Any]:
    """Build a structured artifact when setup or judge execution fails."""
    now = datetime.now(UTC).isoformat()
    reason = f"{type(exc).__name__}: {exc}"
    return {
        "run": {
            "started_at": now,
            "ended_at": now,
            "duration_ms": 0.0,
            "evaluation_scope": "optional RAGAS quality regression failed before scoring",
            "cases_path": str(args.cases),
            "docs_dir": str(args.docs_dir),
            "mode": args.mode,
            "answer_source": args.answer_source,
            "metric_profile": getattr(args, "metrics_profile", DEFAULT_METRIC_PROFILE),
            "repeat_count": getattr(args, "repeat_count", 1),
            "judge_execution": {
                "status": "failed",
                "reason": reason,
            },
            "human_review_path": str(getattr(args, "human_review", "")),
            "top_k": args.top_k,
            "min_score": args.min_score,
            "case_ids": [],
            "metrics": metrics_for_profile(
                getattr(args, "metrics_profile", DEFAULT_METRIC_PROFILE)
            ),
            "supported_metrics": RAGAS_METRICS,
            "ragas_version": safe_package_version("ragas"),
            "datasets_version": safe_package_version("datasets"),
            "judge_model": config.effective_ragas_eval_model,
            "embedding_model": config.effective_ragas_eval_embedding_model,
            "temperature": 0,
            "environment": collect_eval_environment(suite="ragas"),
        },
        "thresholds": ragas_thresholds(),
        "summary": {
            "status": "failed",
            "case_count": 0,
            "quality_case_count": 0,
            "refusal_case_count": 0,
            "passed_count": 0,
            "pass_rate": 0.0,
            "core_case_count": 0,
            "core_case_pass_rate": 0.0,
            "refusal_boundary_rate": 0.0,
            "faithfulness_avg": 0.0,
            "response_relevancy_avg": 0.0,
            "id_context_precision_avg": 0.0,
            "id_context_recall_avg": 0.0,
            "oncall_actionability_avg": 0.0,
            "failed_cases": [
                {
                    "suite": "ragas",
                    "id": "ragas_setup",
                    "failed_metrics": ["ragas_setup"],
                    "failure_reasons": {"ragas_setup": reason},
                    "retrieved_sources": [],
                    "expected_sources": [],
                    "suggested_backlog_category": "tool_failure",
                    "judge_model": config.effective_ragas_eval_model,
                }
            ],
        },
        "human_review_comparison": {
            "status": "not_run",
            "reviewed_case_count": 0,
            "available_review_count": 0,
            "agreement_rate": None,
            "comparisons": [],
        },
        "case_scores": [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
