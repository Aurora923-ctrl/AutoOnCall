"""Offline RAG retrieval evaluation for Runbook coverage and rejection behavior."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.document_splitter_service import document_splitter_service
from app.services.rag_retrieval_service import document_to_retrieval_chunk
from scripts.eval.eval_environment import collect_eval_environment

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "rag_cases.yaml"
DEFAULT_DOCS_DIR = REPO_ROOT / "aiops-docs"
DEFAULT_SUMMARY_JSON_PATH = REPO_ROOT / "logs" / "rag_eval_summary.json"
DEFAULT_SUMMARY_MD_PATH = REPO_ROOT / "logs" / "rag_eval_summary.md"
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 2.0

RAG_METRIC_FAILURE_REASONS = {
    "recall_at_k": "Top-K 检索结果未命中期望 Runbook 来源。",
    "keyword_hit": "检索结果未覆盖 case 要求的关键证据词。",
    "citation_coverage": "成功检索 case 缺少 source_file + chunk_id 引用信息。",
    "no_answer_rejection": "无答案 case 未被拒答，仍返回了 Runbook 片段。",
}

DOMAIN_TERMS = {
    "5xx",
    "503",
    "api",
    "cpu",
    "docker",
    "full",
    "gc",
    "inode",
    "jvm",
    "panic",
    "mq",
    "mysql",
    "oom",
    "oomkilled",
    "p95",
    "pod",
    "redis",
    "sql",
    "timeout",
    "下游",
    "不可用",
    "依赖服务",
    "内存",
    "响应慢",
    "外部api",
    "大文件",
    "年假",
    "慢sql",
    "慢查询",
    "报销",
    "接口失败",
    "无答案",
    "服务",
    "服务不可用",
    "死循环",
    "清理",
    "磁盘",
    "缓存",
    "缓存失效",
    "缓存穿透",
    "火焰图",
    "容器日志",
    "业务数据",
    "连接池",
    "配置错误",
    "超时",
}


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load RAG evaluation cases from YAML."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No RAG eval cases found in {path}")
    return [dict(case) for case in cases]


def evaluate_cases(
    cases_path: str | Path = DEFAULT_CASES_PATH,
    *,
    docs_dir: str | Path = DEFAULT_DOCS_DIR,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict[str, Any]:
    """Evaluate all offline RAG retrieval cases."""
    started_at = datetime.now(UTC)
    started_timer = time.perf_counter()
    cases = load_cases(cases_path)
    index = build_offline_index(docs_dir)
    results = [evaluate_case(case, index, top_k=top_k, min_score=min_score) for case in cases]
    non_reject_results = [result for result in results if not result["should_reject"]]
    reject_results = [result for result in results if result["should_reject"]]

    recall_at_1 = _ratio(
        sum(1 for result in non_reject_results if result["recall_at_1"]), len(non_reject_results)
    )
    recall_at_k = _ratio(
        sum(1 for result in non_reject_results if result["recall_at_k"]), len(non_reject_results)
    )
    strict_recall_at_k = _ratio(
        sum(1 for result in non_reject_results if result["strict_recall_at_k"]),
        len(non_reject_results),
    )
    keyword_hit_rate = _ratio(
        sum(1 for result in non_reject_results if result["keyword_hit"]),
        len(non_reject_results),
    )
    citation_results = [result for result in non_reject_results if result["citation_required"]]
    citation_coverage_rate = _ratio(
        sum(1 for result in citation_results if result["citation_hit"]),
        len(citation_results),
    )
    confusion_results = [
        result for result in non_reject_results if result["case_type"] == "confusion"
    ]
    confusion_case_pass_rate = _ratio(
        sum(1 for result in confusion_results if result["passed"]),
        len(confusion_results),
    )
    rejection_rate = _ratio(
        sum(1 for result in reject_results if result["rejection_hit"]),
        len(reject_results),
    )
    mrr = round(
        sum(result["reciprocal_rank"] for result in non_reject_results)
        / max(len(non_reject_results), 1),
        4,
    )

    summary = {
        "case_count": len(results),
        "passed_count": sum(1 for result in results if result["passed"]),
        "pass_rate": _ratio(sum(1 for result in results if result["passed"]), len(results)),
        "recall_at_1": recall_at_1,
        "recall_at_k": recall_at_k,
        "strict_recall_at_k": strict_recall_at_k,
        "top_k": top_k,
        "mrr": mrr,
        "keyword_hit_rate": keyword_hit_rate,
        "citation_coverage_rate": citation_coverage_rate,
        "no_answer_rejection_rate": rejection_rate,
        "confusion_case_pass_rate": confusion_case_pass_rate,
        "non_reject_case_count": len(non_reject_results),
        "reject_case_count": len(reject_results),
        "citation_case_count": len(citation_results),
        "confusion_case_count": len(confusion_results),
        "evaluated_metrics": [
            "recall_at_1",
            "recall_at_k",
            "strict_recall_at_k",
            "mrr",
            "keyword_hit_rate",
            "citation_coverage_rate",
            "no_answer_rejection_rate",
            "confusion_case_pass_rate",
        ],
        "failed_cases": [
            {
                "id": result["id"],
                "case_type": result["case_type"],
                "failed_metrics": result["failed_metrics"],
                "failure_reasons": result["failure_reasons"],
                "retrieved_sources": result["retrieved_sources"],
                "expected_sources": result["expected_sources"],
            }
            for result in results
            if not result["passed"]
        ],
    }
    return {
        "run": {
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "duration_ms": round((time.perf_counter() - started_timer) * 1000, 2),
            "evaluation_scope": (
                "offline deterministic RAG retrieval regression; local Markdown runbooks "
                "and lexical scoring are used, not live LLM or Milvus"
            ),
            "cases_path": str(Path(cases_path)),
            "docs_dir": str(Path(docs_dir)),
            "top_k": top_k,
            "min_score": min_score,
            "case_ids": [str(case.get("id", "")) for case in cases],
            "environment": collect_eval_environment(suite="rag"),
        },
        "summary": summary,
        "cases": results,
    }


def evaluate_case(
    case: dict[str, Any],
    index: list[dict[str, Any]],
    *,
    top_k: int,
    min_score: float,
) -> dict[str, Any]:
    """Evaluate one RAG retrieval case against the offline index."""
    query = str(case.get("query") or "")
    should_reject = bool(case.get("should_reject", False))
    case_type = str(case.get("case_type") or "").strip()
    threshold = float(case.get("min_score", min_score))
    case_index = _index_with_case_fixture(index, case)
    retrieved = search_offline(case_index, query, top_k=top_k, min_score=threshold)
    expected_sources = _expected_sources(case)

    if should_reject:
        rejection_hit = len(retrieved) == 0
        failed_metrics = [] if rejection_hit else ["no_answer_rejection"]
        return {
            "id": case["id"],
            "query": query,
            "case_type": case_type or "negative",
            "should_reject": True,
            "passed": rejection_hit,
            "failed_metrics": failed_metrics,
            "failure_reasons": failure_reasons(failed_metrics),
            "rejection_hit": rejection_hit,
            "citation_required": False,
            "citation_hit": True,
            "recall_at_1": False,
            "recall_at_k": False,
            "reciprocal_rank": 0.0,
            "keyword_hit": True,
            "expected_sources": [],
            "retrieved_sources": [item["source_file"] for item in retrieved],
            "top_score": retrieved[0]["offline_score"] if retrieved else 0.0,
        }

    rank = _first_expected_rank(retrieved, expected_sources)
    keyword_hit = _retrieved_text_has_keywords(retrieved, case.get("expected_keywords", []))
    citation_required = bool(case.get("requires_citation", True))
    citation_hit = (not citation_required) or _retrieved_has_valid_citation(retrieved)
    recall_at_1 = rank == 1
    recall_at_k = rank > 0
    strict_recall_at_k = _all_expected_sources_hit(retrieved, expected_sources)
    reciprocal_rank = round(1 / rank, 4) if rank > 0 else 0.0
    passed = recall_at_k and keyword_hit and citation_hit
    failed_metrics = []
    if not recall_at_k:
        failed_metrics.append("recall_at_k")
    if not keyword_hit:
        failed_metrics.append("keyword_hit")
    if not citation_hit:
        failed_metrics.append("citation_coverage")

    return {
        "id": case["id"],
        "query": query,
        "case_type": case_type or "positive",
        "should_reject": False,
        "passed": passed,
        "failed_metrics": failed_metrics,
        "failure_reasons": failure_reasons(failed_metrics),
        "rejection_hit": False,
        "citation_required": citation_required,
        "citation_hit": citation_hit,
        "recall_at_1": recall_at_1,
        "recall_at_k": recall_at_k,
        "strict_recall_at_k": strict_recall_at_k,
        "reciprocal_rank": reciprocal_rank,
        "keyword_hit": keyword_hit,
        "expected_sources": expected_sources,
        "retrieved_sources": [item["source_file"] for item in retrieved],
        "top_score": retrieved[0]["offline_score"] if retrieved else 0.0,
    }


def build_offline_index(docs_dir: str | Path = DEFAULT_DOCS_DIR) -> list[dict[str, Any]]:
    """Build a local chunk index from Markdown runbooks without Milvus or embeddings."""
    root = Path(docs_dir)
    chunks: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        docs = document_splitter_service.split_document(content, path.as_posix())
        for rank, document in enumerate(docs, 1):
            chunk = document_to_retrieval_chunk(document, score=None, rank=rank)
            searchable_text = " ".join(
                [
                    str(chunk.get("source_file") or ""),
                    str(chunk.get("heading_path") or ""),
                    str(chunk.get("content") or ""),
                ]
            )
            chunk["offline_terms"] = extract_terms(searchable_text)
            chunks.append(chunk)
    if not chunks:
        raise ValueError(f"No Markdown runbooks found in {root}")
    return chunks


def _index_with_case_fixture(
    index: list[dict[str, Any]],
    case: dict[str, Any],
) -> list[dict[str, Any]]:
    fixture_chunk = _fixture_to_chunk(case)
    if fixture_chunk is None:
        return index
    return [fixture_chunk, *index]


def _fixture_to_chunk(case: dict[str, Any]) -> dict[str, Any] | None:
    fixture = case.get("fixture")
    if not isinstance(fixture, dict):
        return None
    metadata = dict(fixture.get("metadata") or {})
    metadata.setdefault("_file_name", fixture.get("source_file", "fixture"))
    metadata.setdefault("_chunk_id", fixture.get("chunk_id", "fixture#0001"))
    metadata.setdefault("_source", fixture.get("source_file", "fixture"))
    document = type(
        "OfflineFixtureDocument",
        (),
        {
            "page_content": str(fixture.get("content") or ""),
            "metadata": metadata,
        },
    )()
    chunk = document_to_retrieval_chunk(document, score=None, rank=1)
    chunk["heading_path"] = str(fixture.get("heading_path") or chunk.get("heading_path") or "")
    searchable_text = " ".join(
        [
            str(chunk.get("source_file") or ""),
            str(chunk.get("heading_path") or ""),
            str(chunk.get("content") or ""),
        ]
    )
    chunk["offline_terms"] = extract_terms(searchable_text)
    return chunk


def search_offline(
    index: list[dict[str, Any]],
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Search the offline index with deterministic lexical scoring."""
    query_terms = extract_terms(query)
    scored = []
    for chunk in index:
        score = lexical_score(query, query_terms, chunk)
        if score < min_score:
            continue
        ranked = dict(chunk)
        ranked["offline_score"] = round(score, 4)
        ranked.pop("offline_terms", None)
        scored.append(ranked)
    scored.sort(key=lambda item: (-item["offline_score"], item["source_file"], item["chunk_id"]))
    return scored[:top_k]


def lexical_score(query: str, query_terms: set[str], chunk: dict[str, Any]) -> float:
    """Score a chunk with token overlap and domain phrase bonuses."""
    chunk_terms = set(chunk.get("offline_terms") or set())
    overlap = query_terms & chunk_terms
    score = 0.0
    for term in overlap:
        score += 3.0 if term in DOMAIN_TERMS else 1.0

    query_text = query.lower()
    chunk_text = f"{chunk.get('heading_path', '')}\n{chunk.get('content', '')}".lower()
    for term in DOMAIN_TERMS:
        if term in query_text and term in chunk_text:
            score += 2.0

    if not query_terms:
        return 0.0
    return score / math.sqrt(len(query_terms))


def extract_terms(text: str) -> set[str]:
    """Extract mixed Chinese and ASCII terms for deterministic offline scoring."""
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.update("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    terms.update("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    terms.update(term for term in DOMAIN_TERMS if term in lowered)
    return {term for term in terms if term.strip()}


def render_summary(payload: dict[str, Any]) -> str:
    """Render a compact text summary for CLI and CI logs."""
    summary = payload["summary"]
    lines = [
        (
            f"RAG eval: {summary['passed_count']}/{summary['case_count']} cases passed "
            f"({summary['pass_rate']:.0%}); "
            f"recall@1={summary['recall_at_1']:.0%}, "
            f"recall@{summary['top_k']}={summary['recall_at_k']:.0%}, "
            f"strict_recall@{summary['top_k']}={summary['strict_recall_at_k']:.0%}, "
            f"MRR={summary['mrr']:.2f}, "
            f"cite={summary['citation_coverage_rate']:.0%}, "
            f"confusion={summary['confusion_case_pass_rate']:.0%}, "
            f"reject={summary['no_answer_rejection_rate']:.0%}"
        )
    ]
    for result in payload["cases"]:
        status = "PASS" if result["passed"] else "FAIL"
        retrieved = ",".join(result["retrieved_sources"][:3]) or "none"
        lines.append(
            f"- {status} {result['id']} top_score={result['top_score']:.2f} sources={retrieved}"
        )
    return "\n".join(lines)


def render_markdown_summary(payload: dict[str, Any]) -> str:
    """Render a reproducible Markdown RAG eval summary."""
    run = payload["run"]
    summary = payload["summary"]
    failed_cases = summary["failed_cases"]
    environment = run.get("environment", {})
    lines = [
        "# AutoOnCall RAG 离线评测摘要",
        "",
        "## 运行记录",
        f"- 生成时间：{run.get('ended_at', '')}",
        f"- case 文件：`{run.get('cases_path', '')}`",
        f"- 文档目录：`{run.get('docs_dir', '')}`",
        f"- 总耗时：{run.get('duration_ms', 0.0):.2f} ms",
        f"- 评测边界：{run.get('evaluation_scope', '')}",
        f"- Git commit：`{environment.get('git_commit', '')}`",
        f"- Python：`{environment.get('python_version', '')}`",
        f"- RAG top_k：{run.get('top_k', summary.get('top_k', 0))}",
        "",
        "## 核心指标",
        f"- RAG case：{summary['passed_count']}/{summary['case_count']} ({summary['pass_rate']:.0%})",
        f"- recall@1：{summary['recall_at_1']:.0%}",
        f"- recall@{summary['top_k']}：{summary['recall_at_k']:.0%}",
        f"- strict recall@{summary['top_k']}：{summary['strict_recall_at_k']:.0%}",
        f"- MRR：{summary['mrr']:.2f}",
        f"- citation coverage：{summary['citation_coverage_rate']:.0%}",
        f"- confusion case pass：{summary['confusion_case_pass_rate']:.0%}",
        f"- no-answer rejection：{summary['no_answer_rejection_rate']:.0%}",
        "",
        "> 以上指标只代表离线固定 case 的检索回归结果，不代表线上问答准确率。",
        "",
        "## 失败定位",
    ]
    if failed_cases:
        for item in failed_cases:
            lines.append(
                f"- {item['id']}：{', '.join(item['failed_metrics'])}；"
                f"期望来源：{', '.join(item.get('expected_sources', [])) or '-'}；"
                f"实际来源：{', '.join(item.get('retrieved_sources', [])) or '-'}"
            )
    else:
        lines.append("- 无失败 case。")

    lines.extend(
        [
            "",
            "## Case 明细",
            "| Case | 类型 | 结果 | 期望来源 | 实际来源 | Top score | 失败指标 |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for result in payload["cases"]:
        lines.append(
            "| "
            f"{result['id']} | "
            f"{result['case_type']} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | "
            f"{', '.join(result.get('expected_sources', [])) or '-'} | "
            f"{', '.join(result.get('retrieved_sources', [])) or '-'} | "
            f"{result.get('top_score', 0.0):.2f} | "
            f"{', '.join(result.get('failed_metrics', [])) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def write_eval_artifacts(
    payload: dict[str, Any],
    *,
    summary_json_path: str | Path | None,
    summary_md_path: str | Path | None,
) -> dict[str, str]:
    """Write JSON and Markdown RAG eval summaries."""
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


def failure_reasons(failed_metrics: list[str]) -> dict[str, str]:
    """Map failed RAG metric names to human-readable reasons."""
    return {metric: RAG_METRIC_FAILURE_REASONS.get(metric, metric) for metric in failed_metrics}


def _expected_sources(case: dict[str, Any]) -> list[str]:
    if case.get("expected_sources"):
        return [str(item) for item in case["expected_sources"]]
    source = case.get("expected_source")
    return [str(source)] if source else []


def _first_expected_rank(retrieved: list[dict[str, Any]], expected_sources: list[str]) -> int:
    for index, item in enumerate(retrieved, 1):
        if item.get("source_file") in expected_sources:
            return index
    return 0


def _all_expected_sources_hit(retrieved: list[dict[str, Any]], expected_sources: list[str]) -> bool:
    if not expected_sources:
        return False
    retrieved_sources = {str(item.get("source_file") or "") for item in retrieved}
    return all(source in retrieved_sources for source in expected_sources)


def _retrieved_text_has_keywords(
    retrieved: list[dict[str, Any]], expected_keywords: list[str]
) -> bool:
    if not expected_keywords:
        return True
    text = "\n".join(
        f"{item.get('source_file', '')}\n{item.get('heading_path', '')}\n{item.get('content', '')}"
        for item in retrieved
    ).lower()
    return all(str(keyword).lower() in text for keyword in expected_keywords)


def _retrieved_has_valid_citation(retrieved: list[dict[str, Any]]) -> bool:
    for item in retrieved:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if source_file and source_file != "未知来源" and chunk_id:
            return True
    return False


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / max(denominator, 1), 4)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Run offline RAG retrieval evaluation.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--summary-json", default=str(DEFAULT_SUMMARY_JSON_PATH))
    parser.add_argument("--summary-md", default=str(DEFAULT_SUMMARY_MD_PATH))
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text summary")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    payload = evaluate_cases(
        args.cases,
        docs_dir=args.docs_dir,
        top_k=args.top_k,
        min_score=args.min_score,
    )
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
    return 0 if payload["summary"]["passed_count"] == payload["summary"]["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
