"""Strictly score production knowledge assets against the high-value RAG rubric."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from app.services.document_loaders import document_loader_registry
from app.services.document_splitter_service import document_splitter_service

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCS_DIR = ROOT / "docs" / "knowledge-base"

REQUIRED_FIELDS = {
    "scope": ("scope", "适用范围", "适用", "impact", "影响范围"),
    "evidence": ("evidence", "证据", "metric", "指标", "query", "查询"),
    "diagnosis": ("hypothesis", "假设", "decision", "决策", "排除", "root cause", "根因"),
    "action": ("response", "处置", "resolution", "remediation", "恢复", "recovery"),
    "approval": ("approval", "审批", "approver", "人工", "change plan", "变更计划"),
    "rollback": ("rollback", "回滚", "rollback condition", "回滚条件"),
    "ownership": ("owner", "负责人", "责任人"),
    "freshness": ("updated", "reviewed", "更新时间", "复核", "retrieved"),
}

OPERATIONAL_MARKERS = (
    "promql",
    "sql",
    "explain",
    "info clients",
    "kubectl",
    "logql",
    "rate(",
    "histogram_quantile",
    "metric",
    "指标",
    "查询",
)
LOG_MARKERS = ("log", "日志", "error", "timeout", "exception", "pattern", "模式")
EVIDENCE_QUALIFIERS = (
    "time window",
    "incident-window",
    "时间窗口",
    "基线",
    "observation",
    "观察",
)
NOISE_PATTERNS = (
    r"(?m)^---$",
    r"\{\{[%<]",
    r"\]\(/",
    r"(?i)for a curated documentation index",
    r"(?i)click here",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    paths = [
        path
        for path in sorted(args.docs_dir.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
    ]
    results = score_assets(paths)
    payload = {
        "threshold": 9.0,
        "scoring_profile": "strict-high-value-v2",
        "passed": all(item["score"] >= 9.0 and item["grade"] == "PASS" for item in results),
        "asset_count": len(results),
        "results": results,
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for item in results:
        failures = ",".join(item["hard_failures"]) or "-"
        print(
            f"{item['source_file']}\t{item['score']:.1f}/10\t"
            f"chunks={item['chunks']}\t{item['grade']}\tfailures={failures}"
        )
    if not payload["passed"]:
        raise SystemExit(1)


def score_assets(paths: list[Path]) -> list[dict[str, object]]:
    loaded_assets: list[dict[str, Any]] = []
    for path in paths:
        loader = document_loader_registry.get_loader(path)
        loaded, report = loader.load(path)
        chunks = document_splitter_service.split_loaded_documents(loaded, str(path))
        text = "\n".join(str(document.page_content or "") for document in chunks)
        loaded_assets.append(
            {
                "path": path,
                "report": report,
                "chunks": chunks,
                "text": text,
            }
        )
    duplicate_scores = _duplicate_scores(loaded_assets)
    return [
        score_asset(
            item["path"],
            report=item["report"],
            chunks=item["chunks"],
            text=item["text"],
            duplicate_score=duplicate_scores.get(item["path"].name, 0.0),
        )
        for item in loaded_assets
    ]


def score_asset(
    path: Path,
    *,
    report: Any | None = None,
    chunks: list[Any] | None = None,
    text: str | None = None,
    duplicate_score: float = 0.0,
) -> dict[str, object]:
    """Score one asset with semantic hard gates rather than keyword count alone."""
    if report is None or chunks is None or text is None:
        loader = document_loader_registry.get_loader(path)
        loaded, report = loader.load(path)
        chunks = document_splitter_service.split_loaded_documents(loaded, str(path))
        text = "\n".join(str(document.page_content or "") for document in chunks)
    lowered = text.casefold()

    criteria = {
        "parse_and_structure": 1.0 if chunks and not report.warnings else 0.0,
        "semantic_chunking": _semantic_chunk_score(path.suffix.lower(), chunks),
        "operational_density": _operational_density(lowered),
        "diagnostic_reasoning": _diagnostic_score(lowered),
        "actionability": _action_score(lowered),
        "safety_boundary": _safety_score(lowered),
        "ownership_freshness": _metadata_score(lowered),
        "traceability": _traceability_score(path, lowered),
        "specificity": _specificity_score(text, lowered),
        "noise_control": _noise_score(text),
        "independence": round(max(0.0, 1.0 - duplicate_score), 2),
    }

    missing = _missing_required_fields(lowered)
    hard_failures = list(missing)
    if criteria["semantic_chunking"] < 0.8:
        hard_failures.append("semantic_chunking")
    if criteria["noise_control"] < 1.0:
        hard_failures.append("noise")
    if duplicate_score >= 0.45:
        hard_failures.append("cross_document_duplication")
    if path.suffix.lower() == ".pdf":
        hard_failures.extend(_pdf_quality_failures(path))

    raw_score = round(sum(criteria.values()) / len(criteria) * 10, 1)
    score = min(raw_score, 8.8) if hard_failures else min(raw_score, 10.0)
    return {
        "source_file": path.name,
        "format": path.suffix.lower(),
        "score": score,
        "grade": "PASS" if score >= 9.0 and not hard_failures else "REPAIR",
        "chunks": len(chunks),
        "characters": len(text),
        "criteria": criteria,
        "warnings": list(report.warnings),
        "hard_failures": sorted(set(hard_failures)),
        "missing_required_fields": missing,
        "duplicate_score": duplicate_score,
    }


def _semantic_chunk_score(suffix: str, chunks: list[Any]) -> float:
    maximum = 15 if suffix == ".xlsx" else 12
    if not 5 <= len(chunks) <= maximum:
        return 0.0
    meaningful = 0
    for chunk in chunks:
        content = str(chunk.page_content or "").strip()
        ascii_tokens = set(
            re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", content.casefold())
        )
        cjk_count = len(re.findall(r"[\u4e00-\u9fff]", content))
        heading = str(
            chunk.metadata.get("heading_path")
            or chunk.metadata.get("_heading_path")
            or chunk.metadata.get("h2")
            or chunk.metadata.get("h1")
            or ""
        ).strip()
        rich_enough = len(ascii_tokens) >= 8 or cjk_count >= 35
        if len(content) >= 100 and rich_enough and (heading or suffix in {".xlsx", ".pdf"}):
            meaningful += 1
    return round(meaningful / len(chunks), 2) if chunks else 0.0


def _operational_density(text: str) -> float:
    operational_hits = sum(marker in text for marker in OPERATIONAL_MARKERS)
    log_hits = sum(marker in text for marker in LOG_MARKERS)
    if operational_hits < 2 or not log_hits:
        return 0.0
    return min(1.0, 0.35 + 0.1 * operational_hits + 0.1 * min(log_hits, 3))


def _diagnostic_score(text: str) -> float:
    hypothesis = len(re.findall(r"hypothesis|假设", text))
    exclusion = len(re.findall(r"reject|排除|反证|not primary|非主要|未确认", text))
    decision = len(re.findall(r"decision|决策树|判断|区分|根因", text))
    return min(
        1.0,
        0.25 * min(hypothesis, 3)
        + 0.25 * min(exclusion, 3)
        + 0.2 * min(decision, 3),
    )


def _action_score(text: str) -> float:
    action = len(re.findall(r"处置|resolution|remediation|recovery|恢复|change|变更", text))
    verify = len(re.findall(r"验证|verify|canary|灰度|观察|observe", text))
    return min(1.0, 0.25 * min(action, 3) + 0.2 * min(verify, 3))


def _safety_score(text: str) -> float:
    approval = len(re.findall(r"approval|审批|人工|approver|变更计划", text))
    rollback = len(re.findall(r"rollback|回滚", text))
    return min(1.0, 0.3 * min(approval, 3) + 0.25 * min(rollback, 3))


def _metadata_score(text: str) -> float:
    ownership = any(term in text for term in REQUIRED_FIELDS["ownership"])
    freshness = any(term in text for term in REQUIRED_FIELDS["freshness"])
    return (float(ownership) + float(freshness)) / 2


def _traceability_score(path: Path, text: str) -> float:
    upstream = "source url" in text and ("revision" in text or "commit" in text)
    incident = any(term in text for term in ("ticket", "工单", "incident", "cr-", "inc-"))
    if path.name.startswith("official_"):
        return 1.0 if upstream else 0.0
    return 1.0 if incident else 0.5


def _specificity_score(text: str, lowered: str) -> float:
    qualified = sum(marker in lowered for marker in EVIDENCE_QUALIFIERS)
    numbers = len(re.findall(r"\b\d+(?:\.\d+)?(?:%|ms|s|mb|gb)?\b", text))
    return min(1.0, 0.25 + 0.15 * min(qualified, 3) + 0.1 * min(numbers, 5))


def _missing_required_fields(text: str) -> list[str]:
    missing = [
        name
        for name, terms in REQUIRED_FIELDS.items()
        if not any(term.casefold() in text for term in terms)
    ]
    if not any(marker in text for marker in OPERATIONAL_MARKERS):
        missing.append("operational_query")
    if not any(marker in text for marker in LOG_MARKERS):
        missing.append("log_pattern")
    return missing


def _duplicate_scores(assets: list[dict[str, Any]]) -> dict[str, float]:
    shingles: dict[str, set[str]] = {}
    for item in assets:
        tokens = re.findall(
            r"[a-z0-9_:-]{3,}|[\u4e00-\u9fff]{2,}",
            item["text"].casefold(),
        )
        shingles[item["path"].name] = {
            " ".join(tokens[index : index + 4])
            for index in range(max(0, len(tokens) - 3))
        }
    scores: Counter[str] = Counter()
    for left, right in combinations(shingles, 2):
        union = shingles[left] | shingles[right]
        similarity = len(shingles[left] & shingles[right]) / len(union) if union else 0.0
        if similarity >= 0.35:
            scores[left] = max(scores[left], similarity)
            scores[right] = max(scores[right], similarity)
    return {name: round(float(scores.get(name, 0.0)), 2) for name in shingles}


def _pdf_quality_failures(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=True)
        if len(reader.pages) < 3:
            return ["pdf_page_count"]
        if not all((page.extract_text() or "").strip() for page in reader.pages):
            return ["pdf_empty_page"]
        return []
    except Exception as exc:
        return [f"pdf_parse:{type(exc).__name__}"]


def _noise_score(text: str) -> float:
    return 0.0 if any(re.search(pattern, text) for pattern in NOISE_PATTERNS) else 1.0


if __name__ == "__main__":
    main()
