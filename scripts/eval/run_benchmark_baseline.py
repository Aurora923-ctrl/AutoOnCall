"""Run a timestamped, provenance-rich local benchmark without overwriting history."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.benchmark_metrics import proportion_metric
from scripts.eval.eval_environment import (
    assess_eval_artifact_staleness,
    collect_eval_environment,
    provenance_markdown_lines,
)

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "logs" / "benchmarks"

MODULES = [
    {
        "id": "knowledge_quality",
        "evidence_level": "local_live",
        "script": "scripts/eval/eval_knowledge_quality.py",
        "extra_args": [],
    },
    {
        "id": "aiops",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/eval_cases.py",
        "extra_args": [
            "--cases",
            "eval/cases.yaml",
            "--env-file",
            "deploy/sandbox.env",
            "--report-path",
            "eval_reports.db",
        ],
    },
    {
        "id": "rag",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/eval_rag_cases.py",
        "extra_args": [
            "--cases",
            "eval/rag_cases.yaml",
            "--docs-dir",
            "docs/knowledge-base",
            "--top-k",
            "3",
        ],
    },
    {
        "id": "ragas",
        "evidence_level": "offline_fixture",
        "required": False,
        "script": "scripts/eval/eval_ragas_cases.py",
        "extra_args": ["--cases", "eval/rag_cases.yaml", "--docs-dir", "docs/knowledge-base"],
    },
    {
        "id": "safe_change",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/eval_change_cases.py",
        "extra_args": ["--cases", "eval/change_cases.yaml"],
    },
    {
        "id": "replanner",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/eval_replanner_cases.py",
        "extra_args": ["--cases", "eval/replanner_cases.yaml"],
    },
    {
        "id": "api_contract",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/verify_api_contracts.py",
        "extra_args": [],
    },
    {
        "id": "performance",
        "evidence_level": "offline_fixture",
        "script": "scripts/eval/eval_performance.py",
        "extra_args": ["--limit", "50", "--evidence-level", "unclassified"],
    },
]


def run_baseline(
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    skip_milvus: bool = False,
    force_candidate: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Run all local modules into a unique history directory."""
    started_at = datetime.now(UTC)
    environment = collect_eval_environment(
        suite="benchmark_baseline",
        evidence_level="local_live",
    )
    run_id = build_run_id(started_at, environment)
    run_dir = reserve_run_directory(output_root, run_id)
    modules = [run_module(spec, run_dir=run_dir, skip_milvus=skip_milvus) for spec in MODULES]
    required_modules = [item for item in modules if item.get("required", True)]
    optional_modules = [item for item in modules if not item.get("required", True)]
    passed_count = sum(1 for item in modules if item["status"] == "passed")
    required_passed_count = sum(1 for item in required_modules if item["status"] == "passed")
    missing_count = sum(1 for item in modules if item["status"] == "missing")
    incomplete_count = sum(1 for item in modules if item["status"] == "incomplete")
    failed_count = sum(
        1 for item in modules if item["status"] not in {"passed", "missing", "incomplete"}
    )
    stale_count = sum(1 for item in required_modules if item["artifact_status"]["stale"])
    official_block_reasons = build_official_block_reasons(
        environment=environment,
        modules=modules,
    )
    if force_candidate:
        official_block_reasons.insert(0, "candidate_requested")
    complete = required_passed_count == len(required_modules) and stale_count == 0
    environment_changed = any(module["artifact_status"]["stale"] for module in required_modules)
    official_baseline = complete and not official_block_reasons
    summary = {
        "status": "passed" if complete else "failed",
        "official_baseline": official_baseline,
        "baseline_status": (
            "official"
            if official_baseline
            else (
                "candidate_environment_changed"
                if environment_changed
                else (
                    "candidate_dirty_worktree"
                    if environment.get("git_dirty")
                    else "candidate_incomplete"
                )
            )
        ),
        "module_count": len(modules),
        "passed_module_count": passed_count,
        "required_module_count": len(required_modules),
        "required_passed_module_count": required_passed_count,
        "optional_module_count": len(optional_modules),
        "failed_module_count": failed_count,
        "missing_module_count": missing_count,
        "incomplete_module_count": incomplete_count,
        "stale_module_count": stale_count,
        "metrics": {
            "module_pass_rate": proportion_metric(
                numerator=required_passed_count,
                denominator=len(required_modules),
                label="Required benchmark module pass rate",
                source="modules[required=true].status",
            )
        },
        "official_block_reasons": official_block_reasons,
    }
    payload = {
        "run": {
            "run_id": run_dir.name,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "command": " ".join(sys.argv),
            "output_dir": _relative_or_absolute(run_dir),
            "evidence_levels": [
                "offline_fixture",
                "local_live",
                "controlled_fault",
                "production",
            ],
            "environment": environment,
        },
        "summary": summary,
        "modules": modules,
    }
    manifest_json = run_dir / "baseline_manifest.json"
    manifest_md = run_dir / "baseline_manifest.md"
    manifest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    manifest_md.write_text(render_markdown(payload), "utf-8")
    write_interview_scorecard(payload, manifest_json=manifest_json, run_dir=run_dir)
    write_latest_pointer(output_root, payload, manifest_json, manifest_md)
    return payload, run_dir


def write_interview_scorecard(
    payload: dict[str, Any],
    *,
    manifest_json: Path,
    run_dir: Path,
) -> None:
    """Generate the stage-8 delivery artifacts from this exact benchmark run."""
    from scripts.eval.build_interview_summary import (
        build_scorecard_from_manifest,
        write_scorecard_outputs,
    )

    scorecard = build_scorecard_from_manifest(payload, manifest_path=manifest_json)
    write_scorecard_outputs(scorecard, run_dir=run_dir)


def run_module(
    spec: dict[str, Any],
    *,
    run_dir: Path,
    skip_milvus: bool,
) -> dict[str, Any]:
    """Execute one benchmark module and inspect its raw artifact."""
    module_id = str(spec["id"])
    json_path = run_dir / f"{module_id}.json"
    md_path = run_dir / f"{module_id}.md"
    extra_args = list(spec.get("extra_args") or [])
    if module_id == "knowledge_quality" and skip_milvus:
        extra_args.append("--skip-milvus")
    extra_args = [str(run_dir / item) if item == "eval_reports.db" else item for item in extra_args]
    command = [
        sys.executable,
        str(REPO_ROOT / str(spec["script"])),
        *extra_args,
        "--summary-json",
        str(json_path),
        "--summary-md",
        str(md_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return_code = completed.returncode
        stdout = completed.stdout[-4000:]
        stderr = completed.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        return_code = 124
        stdout = str(exc.stdout or "")[-4000:]
        stderr = f"timeout after {exc.timeout}s"
    artifact = _load_json(json_path)
    status = "missing" if artifact is None else artifact_status(artifact)
    staleness = (
        assess_eval_artifact_staleness(artifact.get("run"))
        if isinstance(artifact, dict)
        else {
            "stale": True,
            "reasons": ["missing_artifact"],
            "generated_fingerprint": "",
            "current_fingerprint": "",
        }
    )
    if return_code != 0 and status == "passed":
        status = "failed"
    return {
        "id": module_id,
        "status": status,
        "evidence_level": spec["evidence_level"],
        "required": bool(spec.get("required", True)),
        "return_code": return_code,
        "command": command,
        "json_path": _relative_or_absolute(json_path),
        "markdown_path": _relative_or_absolute(md_path),
        "stdout": stdout,
        "stderr": stderr,
        "artifact_status": staleness,
        "summary": artifact.get("summary", {}) if isinstance(artifact, dict) else {},
    }


def artifact_status(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return "failed"
    raw = str(summary.get("status") or "").lower()
    if raw in {"passed", "pass", "ready"}:
        return "passed"
    if raw in {
        "passed_without_milvus",
        "observed_not_accepted",
        "retrieval_only_passed",
        "incomplete",
        "not_run",
    }:
        return "incomplete"
    if raw in {"failed", "fail", "not_ready"}:
        return "failed"
    if summary.get("all_passed") is True:
        return "passed"
    passed = summary.get("passed_count", summary.get("overall_passed_count"))
    total = summary.get("case_count", summary.get("overall_case_count"))
    if isinstance(passed, int | float) and isinstance(total, int | float) and total:
        return "passed" if passed == total else "failed"
    return "failed"


def build_official_block_reasons(
    *,
    environment: dict[str, Any],
    modules: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if environment.get("git_dirty"):
        reasons.append("dirty_worktree")
    if not environment.get("git_commit"):
        reasons.append("missing_git_commit")
    for module in modules:
        if not module.get("required", True):
            continue
        if module["status"] != "passed":
            reasons.append(f"{module['id']}:{module['status']}")
        if module["artifact_status"]["stale"]:
            reasons.append(f"{module['id']}:stale")
    return list(dict.fromkeys(reasons))


def build_run_id(started_at: datetime, environment: dict[str, Any]) -> str:
    commit = str(environment.get("git_commit") or "nogit")[:8]
    fingerprint = str(environment.get("evaluation_fingerprint") or "unknown")[:8]
    return f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{commit}-{fingerprint}"


def reserve_run_directory(output_root: Path, run_id: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    candidate = output_root / run_id
    suffix = 1
    while candidate.exists():
        candidate = output_root / f"{run_id}-{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=False)
    return candidate


def write_latest_pointer(
    output_root: Path,
    payload: dict[str, Any],
    manifest_json: Path,
    manifest_md: Path,
) -> None:
    """Update lightweight latest pointers without touching historical artifacts."""
    pointer = {
        "run_id": payload["run"]["run_id"],
        "status": payload["summary"]["status"],
        "official_baseline": payload["summary"]["official_baseline"],
        "baseline_status": payload["summary"]["baseline_status"],
        "manifest_json": _relative_or_absolute(manifest_json),
        "manifest_md": _relative_or_absolute(manifest_md),
        "generated_at": payload["run"]["ended_at"],
    }
    (output_root / "latest.json").write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2),
        "utf-8",
    )
    shutil.copyfile(manifest_md, output_root / "latest.md")


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# AutoOnCall Benchmark Baseline",
        "",
        "## Baseline",
        "",
        f"- Run ID: `{payload['run']['run_id']}`",
        f"- Status: `{summary['status']}`",
        f"- Baseline status: `{summary['baseline_status']}`",
        f"- Official baseline: `{summary['official_baseline']}`",
        (
            f"- Required modules: `{summary['required_passed_module_count']}/"
            f"{summary['required_module_count']}`"
        ),
        f"- Optional modules: `{summary['optional_module_count']}`",
        f"- Block reasons: `{', '.join(summary['official_block_reasons']) or 'none'}`",
        *provenance_markdown_lines(payload["run"]["environment"]),
        "",
        "## Modules",
        "",
        "| Module | Required | Evidence | Status | Stale | Artifact |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for module in payload["modules"]:
        lines.append(
            f"| `{module['id']}` | `{module.get('required', True)}` | "
            f"`{module['evidence_level']}` | "
            f"`{module['status']}` | `{module['artifact_status']['stale']}` | "
            f"`{module['json_path']}` |"
        )
    lines.extend(
        [
            "",
            "## Evidence Levels",
            "",
            "- `offline_fixture`: deterministic fixed cases or fake dependencies.",
            "- `local_live`: real services running in the local controlled environment.",
            "- `controlled_fault`: deliberately injected failure in a controlled environment.",
            "- `production`: real production traffic or incident evidence.",
            "",
            "> A dirty worktree can produce a candidate benchmark, but never an official baseline.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _relative_or_absolute(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--skip-milvus", action="store_true")
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="force candidate status even when the worktree is clean and committed",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload, run_dir = run_baseline(
        output_root=Path(args.output_root),
        skip_milvus=args.skip_milvus,
        force_candidate=args.candidate,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Benchmark baseline: "
            f"{payload['summary']['status']}; "
            f"baseline={payload['summary']['baseline_status']}; "
            f"modules={payload['summary']['passed_module_count']}/"
            f"{payload['summary']['module_count']}; "
            f"run_dir={run_dir}"
        )
    return 0 if payload["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
