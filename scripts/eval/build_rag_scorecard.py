"""Build the final candidate RAG scorecard from existing evaluation artifacts."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.eval_environment import collect_eval_environment

DEFAULT_JSON = REPO_ROOT / "logs" / "rag_scorecard_candidate.json"
DEFAULT_MD = REPO_ROOT / "logs" / "rag_scorecard_candidate.md"

ARTIFACTS = {
    "offline_retrieval": REPO_ROOT / "logs" / "rag_stage2_current.json",
    "runtime_retrieval": REPO_ROOT / "logs" / "rag_final_retrieval_candidate.json",
    "fixed_context_generation": REPO_ROOT / "logs" / "ragas_full_generated_core_summary.json",
    "runtime_id_smoke": REPO_ROOT / "logs" / "rag_final_id_smoke_candidate.json",
    "runtime_demo": REPO_ROOT / "logs" / "rag_demo_frozen_runtime_candidate.json",
    "demo_chain": REPO_ROOT / "logs" / "rag_demo_chain_verification_candidate.json",
    "api_contract": REPO_ROOT / "logs" / "rag_final_api_contract_candidate.json",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def artifact_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def build_scorecard() -> dict[str, Any]:
    payloads = {name: read_json(path) for name, path in ARTIFACTS.items()}
    offline = payloads["offline_retrieval"]
    retrieval = payloads["runtime_retrieval"]
    fixed = payloads["fixed_context_generation"]
    runtime = payloads["runtime_id_smoke"]
    demo = payloads["runtime_demo"]
    chain = payloads["demo_chain"]
    api_contract = payloads["api_contract"]

    token_rows = [
        case.get("observability", {}).get("token_usage", {}) for case in demo.get("cases", [])
    ]
    token_usage = {
        "status": demo.get("summary", {}).get("token_usage_status", "not_observed"),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in token_rows),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in token_rows),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in token_rows),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "candidate",
        "official": False,
        "official_blockers": ["dirty_worktree"],
        "scope": (
            "RAG evidence separated into deterministic retrieval, fixed-context generation, "
            "and runtime end-to-end layers."
        ),
        "environment": collect_eval_environment(suite="rag_scorecard"),
        "layers": {
            "deterministic_retrieval": {
                "status": "passed",
                "offline_regression": {
                    "cases": (
                        f"{offline['summary']['passed_count']}/{offline['summary']['case_count']}"
                    ),
                    "recall_at_3": offline["summary"]["recall_at_3"],
                    "mrr": offline["summary"]["mrr"],
                    "ndcg_at_3": offline["summary"]["ndcg_at_3"],
                    "citation_coverage": offline["summary"]["citation_coverage_rate"],
                    "refusal_precision": offline["summary"]["no_answer_rejection_precision"],
                    "refusal_recall": offline["summary"]["no_answer_rejection_recall"],
                    "evidence_boundary": "historical dirty-worktree regression candidate",
                },
                "runtime_frozen_retrieval": {
                    "cases": (
                        f"{retrieval['summary']['passed_count']}/"
                        f"{retrieval['summary']['case_count']}"
                    ),
                    "retrieval_p50_ms": retrieval["summary"]["stage_latency_ms"][
                        "retrieval_total_ms"
                    ]["p50"],
                    "retrieval_p95_ms": retrieval["summary"]["stage_latency_ms"][
                        "retrieval_total_ms"
                    ]["p95"],
                },
            },
            "fixed_context_generation": {
                "status": fixed["summary"]["status"],
                "cases": f"{fixed['summary']['passed_count']}/{fixed['summary']['case_count']}",
                "repeat_count": fixed["run"]["repeat_count"],
                "faithfulness": fixed["summary"]["faithfulness_avg"],
                "response_relevancy": fixed["summary"]["response_relevancy_avg"],
                "id_context_precision": fixed["summary"]["id_context_precision_avg"],
                "id_context_recall": fixed["summary"]["id_context_recall_avg"],
                "oncall_actionability": fixed["summary"]["oncall_actionability_avg"],
                "refusal_boundary": fixed["summary"]["refusal_boundary_rate"],
                "evidence_boundary": (
                    "real qwen-max generation on fixed contexts; failed quality contract"
                ),
            },
            "runtime_end_to_end": {
                "status": runtime["summary"]["status"],
                "id_smoke_cases": (
                    f"{runtime['summary']['passed_count']}/{runtime['summary']['case_count']}"
                ),
                "id_context_precision": runtime["summary"]["id_context_precision_avg"],
                "id_context_recall": runtime["summary"]["id_context_recall_avg"],
                "oncall_actionability": runtime["summary"]["oncall_actionability_avg"],
                "citation_correctness": runtime["summary"]["citation_correctness_rate"],
                "refusal_boundary": runtime["summary"]["refusal_boundary_rate"],
                "frozen_demo_cases": (
                    f"{demo['summary']['passed_count']}/{demo['summary']['case_count']}"
                ),
                "stream_nonstream_cases": (
                    f"{chain['summary']['passed_count']}/{chain['summary']['case_count']}"
                ),
                "stream_nonstream_elapsed_seconds": chain["summary"]["elapsed_seconds"],
                "within_five_minutes": chain["summary"]["within_five_minutes"],
                "models": demo["run"]["models"],
                "stage_latency_ms": demo["summary"]["stage_latency_ms"],
                "token_usage": token_usage,
                "cost": {
                    "status": "not_observed",
                    "reason": "No dated provider price snapshot is frozen.",
                },
                "api_contract": (
                    f"{api_contract['summary']['passed_check_count']}/"
                    f"{api_contract['summary']['check_count']}"
                ),
                "known_failures": [
                    "Full id-smoke contract is 9/12; Disk, MySQL, and Kubernetes miss "
                    "deterministic actionability rubric items.",
                    "CPU remains the explainable context-completeness failure.",
                ],
            },
        },
        "artifacts": {name: artifact_ref(path) for name, path in ARTIFACTS.items()},
    }


def render_markdown(payload: dict[str, Any]) -> str:
    retrieval = payload["layers"]["deterministic_retrieval"]
    fixed = payload["layers"]["fixed_context_generation"]
    runtime = payload["layers"]["runtime_end_to_end"]
    latency = runtime["stage_latency_ms"]
    tokens = runtime["token_usage"]
    lines = [
        "# AutoOnCall RAG Scorecard",
        "",
        f"- Status: `{payload['status']}`; official: `{payload['official']}`",
        "- Boundary: dirty worktree, so every result on this page is a candidate.",
        "",
        "| Layer | Result | Key evidence |",
        "| --- | --- | --- |",
        (
            "| Deterministic retrieval | PASS | "
            f"offline `{retrieval['offline_regression']['cases']}`, "
            f"Recall@3 `{retrieval['offline_regression']['recall_at_3']:.4f}`, "
            f"MRR `{retrieval['offline_regression']['mrr']:.4f}`; "
            f"runtime frozen `{retrieval['runtime_frozen_retrieval']['cases']}` |"
        ),
        (
            "| Fixed-context generation | FAIL candidate | "
            f"`{fixed['cases']}` contract pass, Faithfulness `{fixed['faithfulness']:.4f}`, "
            f"Relevancy `{fixed['response_relevancy']:.4f}`, "
            f"ID recall `{fixed['id_context_recall']:.4f}` |"
        ),
        (
            "| Runtime end-to-end | FAIL full contract / PASS frozen demo | "
            f"id-smoke `{runtime['id_smoke_cases']}`, ID recall "
            f"`{runtime['id_context_recall']:.4f}`, OOD `{runtime['refusal_boundary']:.0%}`; "
            f"frozen demo `{runtime['frozen_demo_cases']}` |"
        ),
        "",
        "## Runtime Snapshot",
        "",
        f"- Stream/non-stream: `{runtime['stream_nonstream_cases']}` in "
        f"`{runtime['stream_nonstream_elapsed_seconds']}s`; within five minutes: "
        f"`{runtime['within_five_minutes']}`.",
        f"- Model: `{runtime['models']['llm']}`; embedding: `{runtime['models']['embedding']}`.",
        f"- Retrieval P50/P95: `{latency['retrieval_total_ms']['p50']}/"
        f"{latency['retrieval_total_ms']['p95']}ms`.",
        f"- Generation P50/P95: `{latency['llm_generation_ms']['p50']}/"
        f"{latency['llm_generation_ms']['p95']}ms`.",
        f"- Total P50/P95: `{latency['total_ms']['p50']}/{latency['total_ms']['p95']}ms`.",
        f"- Provider tokens: input `{tokens['input_tokens']}`, output "
        f"`{tokens['output_tokens']}`, total `{tokens['total_tokens']}`.",
        "- Cost: `not_observed`; no dated provider price snapshot is frozen.",
        f"- API/SSE contracts: `{runtime['api_contract']}`.",
        "",
        "## Known Failures",
        "",
        *[f"- {item}" for item in runtime["known_failures"]],
        "",
        "## Artifact Index",
        "",
        "| Artifact | SHA256 |",
        "| --- | --- |",
    ]
    for item in payload["artifacts"].values():
        lines.append(f"| `{item['path']}` | `{item['sha256']}` |")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-json", default=str(DEFAULT_JSON))
    parser.add_argument("--summary-md", default=str(DEFAULT_MD))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_scorecard()
    Path(args.summary_json).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(args.summary_md).write_text(render_markdown(payload), encoding="utf-8")
    print(render_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
