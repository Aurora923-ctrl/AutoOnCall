"""Optional RAGAS quality evaluation for AutoOnCall RAG answers."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import math
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
    "id_based_context_precision",
    "id_based_context_recall",
]
RAGAS_ID_METRICS = ["id_based_context_precision", "id_based_context_recall"]

MetricRunner = Callable[[list["RagasCaseSample"], dict[str, Any]], dict[str, dict[str, float]]]


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

    started_at = datetime.now(UTC)
    timer = time.perf_counter()
    cases = _select_cases(load_cases(cases_path), max_cases=max_cases)
    offline_index = build_offline_index(docs_dir) if mode == "offline" else None
    agent = (
        RagAgentService(streaming=False)
        if answer_source in {"product-offline", "context-fixture", "runtime"}
        else None
    )
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

    refusal_samples = [sample for sample in samples if is_refusal_case(sample.case)]
    quality_samples = [sample for sample in samples if not is_refusal_case(sample.case)]
    runner_context = build_runner_context(
        mode=mode,
        answer_source=answer_source,
        top_k=top_k,
        min_score=min_score,
        metric_profile=metric_profile,
    )
    metric_runner = metrics_runner or metric_runner_for_profile(metric_profile)
    metric_scores = metric_runner(quality_samples, runner_context) if quality_samples else {}
    results = [
        build_case_result(
            sample,
            metric_scores.get(str(sample.case.get("id")), {}),
            metric_profile=metric_profile,
        )
        for sample in samples
    ]
    summary = build_summary(
        results, quality_samples=quality_samples, refusal_samples=refusal_samples
    )
    quality_contract = build_quality_contract(
        summary,
        results,
        metric_profile=metric_profile,
    )

    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - timer) * 1000, 2),
            "evaluation_scope": (
                "optional RAGAS quality regression for fixed AutoOnCall RAG cases; "
                "not a production accuracy claim"
            ),
            "cases_path": str(Path(cases_path)),
            "docs_dir": str(Path(docs_dir)),
            "mode": mode,
            "answer_source": answer_source,
            "metric_profile": metric_profile,
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
        "quality_contract": quality_contract,
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

    try:
        rag_agent_module.retrieve_structured_knowledge = lambda *_args, **_kwargs: retrieval_payload
        agent.query_grounded = fixture_query_grounded  # type: ignore[method-assign]
        return await agent.query_with_retrieval(
            query,
            session_id=f"ragas-offline-{case.get('id', 'case')}",
        )
    finally:
        rag_agent_module.retrieve_structured_knowledge = original_retrieve
        agent.query_grounded = original_query_grounded  # type: ignore[method-assign]


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
    return RagasCaseSample(
        case=case,
        retrieved_contexts=contexts_from_retrieval(retrieval_payload),
        retrieved_context_ids=context_ids_from_retrieval(retrieval_payload),
        reference_context_ids=reference_context_ids(case),
        answer=str(chat_payload.get("answer") or ""),
        answer_policy=str(
            chat_payload.get("answer_policy") or retrieval_payload.get("answer_policy") or ""
        ),
        no_answer=bool(chat_payload.get("no_answer")),
        citations=[item for item in citation_items if isinstance(item, dict)],
        retrieval=retrieval_payload,
    )


def build_reference_fixture_answer(
    case: dict[str, Any],
    retrieval_payload: dict[str, Any],
    citations: list[dict[str, Any]],
) -> str:
    """Build a deterministic answer fixture for smoke runs."""
    reference = str(case.get("reference_answer") or "").strip()
    if not reference:
        reference = build_fallback_reference_answer(case, retrieval_payload)
    answer = "\n\n".join(
        [
            reference,
            (
                "OnCall decision: check metric/log evidence in the incident-window, "
                "confirm the retrieved source_file/chunk_id, and keep approval boundaries "
                "before rollback, scale, limit, or degradation actions."
            ),
        ]
    )
    return ensure_citation_block(answer, citations)


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
) -> dict[str, dict[str, float]]:
    """Run RAGAS metrics for quality samples."""
    if not samples:
        return {}
    try:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics import (
            Faithfulness,
            IDBasedContextPrecision,
            IDBasedContextRecall,
            ResponseRelevancy,
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
                response=sample.answer,
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
    )
    result = evaluate(
        dataset,
        metrics=[
            Faithfulness(),
            ResponseRelevancy(),
            IDBasedContextPrecision(),
            IDBasedContextRecall(),
        ],
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=False,
        show_progress=False,
    )
    rows = result.to_pandas().to_dict(orient="records")
    return {
        str(sample.case.get("id")): {
            metric: safe_float(row.get(metric))
            for metric in RAGAS_METRICS
            if row.get(metric) is not None
        }
        for sample, row in zip(samples, rows, strict=False)
    }


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


def build_case_result(
    sample: RagasCaseSample,
    scores: dict[str, float],
    *,
    metric_profile: str = "full",
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
            "case_type": str(case.get("case_type") or "negative"),
            "tags": ragas_tags(case),
            "core_case": is_core_case(case),
            "should_reject": True,
            "passed": refusal_hit,
            "metrics": {
                "refusal_boundary_hit": refusal_hit,
                "citation_grounding_hit": len(sample.citations) == 0,
            },
            "failed_metrics": failed_metrics,
            "failure_reasons": failure_reasons,
            "answer_policy": sample.answer_policy,
            "retrieved_context_ids": sample.retrieved_context_ids,
            "reference_context_ids": sample.reference_context_ids,
            "retrieved_sources": retrieved_sources(sample),
            "expected_sources": expected_sources(case),
            "suggested_backlog_category": "hallucination_risk" if failed_metrics else "",
        }

    business_scores = business_metric_scores(sample)
    metrics = {
        **{metric: safe_float(scores.get(metric)) for metric in RAGAS_METRICS},
        **business_scores,
    }
    failed_metrics = failed_quality_metrics(metrics, sample, metric_profile=metric_profile)
    return {
        "id": case_id,
        "case_type": str(case.get("case_type") or "positive"),
        "tags": ragas_tags(case),
        "core_case": is_core_case(case),
        "should_reject": False,
        "passed": not failed_metrics,
        "metrics": metrics,
        "failed_metrics": failed_metrics,
        "failure_reasons": quality_failure_reasons(failed_metrics),
        "answer_policy": sample.answer_policy,
        "retrieved_context_ids": sample.retrieved_context_ids,
        "reference_context_ids": sample.reference_context_ids,
        "retrieved_sources": retrieved_sources(sample),
        "expected_sources": expected_sources(case),
        "suggested_backlog_category": suggested_backlog_category(failed_metrics),
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
        "faithfulness_avg": average_metric(quality_results, "faithfulness"),
        "response_relevancy_avg": average_metric(quality_results, "answer_relevancy"),
        "id_context_precision_avg": average_metric(
            quality_results,
            "id_based_context_precision",
        ),
        "id_context_recall_avg": average_metric(quality_results, "id_based_context_recall"),
        "oncall_actionability_avg": average_metric(quality_results, "oncall_actionability_score"),
        "citation_grounding_rate": average_metric(quality_results, "citation_grounding_hit"),
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
    if metric_profile == "full":
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
    watch_metrics = build_watch_metrics(summary, metric_profile=metric_profile)
    contract_passed = all(gate["status"] in {"passed", "not_applicable"} for gate in hard_gates)
    return {
        "name": "AutoOnCall RAGAS business quality contract",
        "status": "passed" if summary.get("status") == "passed" and contract_passed else "failed",
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


def build_watch_metrics(summary: dict[str, Any], *, metric_profile: str) -> list[dict[str, Any]]:
    """Return reported metrics that are not hard gates in the current profile."""
    thresholds = ragas_thresholds()
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
    lines = [
        (
            f"RAGAS eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"profile={run.get('metric_profile', 'unknown')} "
            f"status={summary['status']} "
            f"contract={contract.get('status', 'unknown')} "
            f"faith={summary['faithfulness_avg']:.2f} "
            f"relevancy={summary['response_relevancy_avg']:.2f} "
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
    lines = [
        "# AutoOnCall RAGAS Quality Summary",
        "",
        "## Run",
        f"- Generated at: `{run.get('ended_at', '')}`",
        f"- Mode: `{run.get('mode', '')}`",
        f"- Answer source: `{run.get('answer_source', '')}`",
        f"- Metric profile: `{run.get('metric_profile', '')}`",
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
        f"- Faithfulness avg: `{summary['faithfulness_avg']:.2f}`",
        f"- Response relevancy avg: `{summary['response_relevancy_avg']:.2f}`",
        f"- ID context precision avg: `{summary['id_context_precision_avg']:.2f}`",
        f"- ID context recall avg: `{summary['id_context_recall_avg']:.2f}`",
        f"- OnCall actionability avg: `{summary['oncall_actionability_avg']:.2f}`",
        f"- Refusal boundary rate: `{summary['refusal_boundary_rate']:.0%}`",
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
) -> dict[str, str]:
    """Write JSON and Markdown RAGAS summaries."""
    written: dict[str, str] = {}
    if summary_json_path:
        written["summary_json"] = str(Path(summary_json_path))
    if summary_md_path:
        written["summary_md"] = str(Path(summary_md_path))
    payload["run"]["artifacts"] = written
    if summary_json_path:
        path = Path(summary_json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary_md_path:
        path = Path(summary_md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown_summary(payload), encoding="utf-8")
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
        "top_k": top_k,
        "min_score": min_score,
        "metric_profile": metric_profile,
        "judge_model": config.effective_ragas_eval_model,
        "embedding_model": config.effective_ragas_eval_embedding_model,
        "api_base": config.dashscope_api_base,
        "temperature": 0,
    }


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
    actionability_hits = sum(1 for item in rubric_items if business_token_overlap(item, answer))
    actionability = ratio(actionability_hits, len(rubric_items)) if rubric_items else 1.0
    domain_hit = business_domain_hit(sample)
    evidence_hit = business_evidence_hit(answer)
    operation_hit = business_operation_hit(answer)
    if rubric_items:
        actionability = min(
            actionability,
            ratio(sum([domain_hit, evidence_hit, operation_hit]), 3),
        )
    citation_grounding = 1.0 if has_answer_citation(sample) else 0.0
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
        "citation_grounding_hit": citation_grounding,
        "incident_boundary_hit": incident_boundary,
        "confusion_disambiguation_hit": confusion,
    }


def business_token_overlap(requirement: str, answer: str) -> bool:
    """Return true when rubric domain terms or CJK ngrams appear in the answer."""
    return any(token in answer for token in extract_business_tokens(requirement))


def business_domain_hit(sample: RagasCaseSample) -> bool:
    """Require the answer to mention the expected incident domain or source."""
    answer = sample.answer.lower()
    expected = " ".join(expected_sources(sample.case)).lower()
    query = str(sample.case.get("query") or "").lower()
    source_terms = {
        "cpu": ["cpu"],
        "memory": ["memory", "oom", "jvm", "gc", "鍐呭瓨"],
        "disk": ["disk", "docker", "inode", "纾佺洏"],
        "slow_response": ["slow", "sql", "mysql", "pool", "鎱", "杩炴帴姹"],
        "service_unavailable": ["503", "5xx", "redis", "mq", "timeout", "渚濊禆", "瓒呮椂"],
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
            "璇佹嵁",
            "鎸囨爣",
            "鏃ュ織",
            "鐭ヨ瘑搴",
            "澶嶇洏",
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
            "限流",
            "扩容",
            "降级",
            "重启",
            "鎺掓煡",
            "纭",
            "鍥炴粴",
            "瀹℃壒",
            "闄愭祦",
            "鎵╁",
            "闄嶇骇",
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
    metrics: dict[str, float],
    sample: RagasCaseSample,
    *,
    metric_profile: str,
) -> list[str]:
    """Return failed metrics using configured thresholds."""
    thresholds = ragas_thresholds()
    failed = []
    checks = {}
    if metric_profile == "full":
        checks.update(
            {
                "faithfulness": thresholds["faithfulness"],
                "answer_relevancy": thresholds["response_relevancy"],
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
        if metrics.get(metric, 0.0) < threshold:
            failed.append(metric)
    if metric_profile == "full" and is_core_case(sample.case):
        for metric in ["faithfulness", "answer_relevancy", "oncall_actionability_score"]:
            if metric not in failed and metrics.get(metric, 0.0) < checks[metric]:
                failed.append(metric)
    return failed


def quality_failure_reasons(failed_metrics: list[str]) -> dict[str, str]:
    """Map failed RAGAS and business metrics to actionable reasons."""
    reasons = {
        "faithfulness": "Answer is not sufficiently supported by retrieved Runbook context.",
        "answer_relevancy": "Answer does not stay focused on the user's OnCall question.",
        "id_based_context_precision": "Retrieved context ids include too many non-reference chunks.",
        "id_based_context_recall": "Retrieved context ids miss expected reference chunks.",
        "oncall_actionability_score": "Answer misses required OnCall actionability rubric items.",
        "citation_grounding_hit": "Answer lacks auditable source_file + chunk_id citations.",
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
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
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
            )
        )
    except Exception as exc:
        payload = build_failed_payload(args, exc)
    written = write_eval_artifacts(
        payload,
        summary_json_path=args.summary_json,
        summary_md_path=args.summary_md,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_summary(payload))
        if written:
            print("Artifacts: " + ", ".join(f"{key}={value}" for key, value in written.items()))
    return 0 if payload["summary"]["status"] == "passed" else 1


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
        "case_scores": [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
