"""Build the fixed AutoOnCall interview demo package.

The package is intentionally deterministic: it generates the three mainline
diagnosis reports and, by default, the offline eval summary that supports the
    "not a one-off demo" claim. Redis/MySQL can be backed by the live adapter
    stack; K8s is intentionally presented as an offline golden regression case.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.demo.generate_demo_reports import (  # noqa: E402
    DEFAULT_DEMO_CASE_IDS,
    generate_demo_reports,
)
from scripts.eval.eval_cases import evaluate_cases, write_eval_artifacts  # noqa: E402

DEFAULT_PACKAGE_DIR = REPO_ROOT / "logs" / "interview_demo"
READINESS_MAX_SCORE = 9.5
READINESS_EVAL_MISSING_CAP = 8.6
READINESS_REPORT_FAILURE_CAP = 8.4
LIVE_SOURCE_ONLY_FAILURE_METRIC = "required_live_sources_hit"


async def build_interview_demo_package(
    *,
    output_dir: str | Path = DEFAULT_PACKAGE_DIR,
    case_ids: list[str] | tuple[str, ...] = DEFAULT_DEMO_CASE_IDS,
    skip_eval: bool = False,
    env_file: str | Path | None = None,
    offline_fixtures: bool = False,
) -> dict[str, Any]:
    """Generate reports, optional eval artifacts, and a top-level README."""
    package_dir = Path(output_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = package_dir / "reports"

    report_summary = await generate_demo_reports(
        case_ids=case_ids,
        output_dir=reports_dir,
        report_db_path=package_dir / "demo_reports.db",
        env_file=None if offline_fixtures else env_file,
    )

    eval_artifacts: dict[str, str] = {}
    eval_summary: dict[str, Any] | None = None
    if not skip_eval:
        eval_payload = await evaluate_cases(report_path=package_dir / "eval_reports.db")
        eval_artifacts = write_eval_artifacts(
            eval_payload,
            summary_json_path=package_dir / "eval_summary.json",
            summary_md_path=package_dir / "eval_summary.md",
        )
        eval_summary = eval_payload.get("summary", {})

    package = {
        "scope": (
            "campus recruiting interview package; deterministic reports and offline eval "
            "artifacts for the AutoOnCall AIOps diagnosis mainline"
        ),
        "output_dir": str(package_dir),
        "env_file": "" if offline_fixtures or not env_file else str(Path(env_file)),
        "offline_fixtures": offline_fixtures,
        "reports": report_summary,
        "eval_artifacts": eval_artifacts,
        "eval_summary": eval_summary,
        "readme": str(package_dir / "README.md"),
    }
    package["readiness_scorecard"] = build_readiness_scorecard(package)
    (package_dir / "package_summary.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (package_dir / "README.md").write_text(render_package_readme(package), encoding="utf-8")
    return package


def build_readiness_scorecard(package: dict[str, Any]) -> dict[str, Any]:
    """Score whether the generated package is ready for a campus interview demo."""
    reports = package.get("reports") or {}
    records = list(reports.get("records") or [])
    eval_summary = package.get("eval_summary") or {}
    eval_artifacts = package.get("eval_artifacts") or {}
    eval_ready = _eval_ready_for_interview(eval_summary)
    required_cases = set(DEFAULT_DEMO_CASE_IDS)
    present_cases = {str(record.get("id")) for record in records}

    checks = [
        _score_check(
            "fixed_mainline_cases",
            required_cases.issubset(present_cases),
            1.2,
            "Redis, MySQL, and K8s golden paths are present.",
        ),
        _score_check(
            "all_demo_reports_passed",
            bool(reports.get("all_passed")) and bool(records),
            1.5,
            "Every generated demo report passes its deterministic case assertions.",
        ),
        _score_check(
            "reports_have_evidence",
            bool(records) and all(int(record.get("evidence_count") or 0) >= 3 for record in records),
            1.2,
            "Reports expose enough evidence for an interview walkthrough.",
        ),
        _score_check(
            "reports_have_tool_calls",
            bool(records) and all(int(record.get("tool_count") or 0) >= 2 for record in records),
            1.0,
            "Reports show the Tool Registry path rather than only final prose.",
        ),
        _score_check(
            "risk_boundary_visible",
            bool(records) and all(str(record.get("risk_policy") or "") for record in records),
            1.0,
            "Risk policy is visible for each demo case.",
        ),
        _score_check(
            "eval_artifacts_present",
            bool(eval_artifacts),
            1.1,
            "Offline eval artifacts are included in the package.",
        ),
        _score_check(
            "eval_ready_for_interview",
            eval_ready["passed"],
            1.4,
            eval_ready["reason"],
        ),
        _score_check(
            "interview_boundary_documented",
            True,
            1.1,
            "README documents demo/offline/live-adapter boundaries.",
        ),
    ]
    raw_score = sum(float(check["weight"]) for check in checks if check["passed"])
    score = min(READINESS_MAX_SCORE, raw_score)
    if not reports.get("all_passed"):
        score = min(score, READINESS_REPORT_FAILURE_CAP)
    if not eval_artifacts:
        score = min(score, READINESS_EVAL_MISSING_CAP)

    if score >= 9.0:
        verdict = "ready_for_main_project_demo"
    elif score >= 8.0:
        verdict = "usable_but_needs_polish"
    else:
        verdict = "not_ready"
    return {
        "score": round(score, 2),
        "max_score": READINESS_MAX_SCORE,
        "verdict": verdict,
        "checks": checks,
        "eval_failure_scope": eval_ready,
        "next_actions": _scorecard_next_actions(checks),
    }


def _score_check(name: str, passed: bool, weight: float, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "weight": weight,
        "reason": reason,
    }


def _scorecard_next_actions(checks: list[dict[str, Any]]) -> list[str]:
    failed = [check for check in checks if not check["passed"]]
    if not failed:
        return [
            "Keep this package as the fixed interview entry.",
            "Refresh it before interviews after code or eval case changes.",
        ]
    actions: list[str] = []
    for check in failed:
        name = str(check["name"])
        if name == "eval_artifacts_present":
            actions.append("Run without --skip-eval before using the package in interviews.")
        elif name == "eval_ready_for_interview":
            actions.append(
                "Fix non-live-source eval failures, or run the live adapter stack before presenting."
            )
        elif name == "all_demo_reports_passed":
            actions.append("Fix failed demo report assertions or narrow the demo case list.")
        elif name == "fixed_mainline_cases":
            actions.append("Include Redis, MySQL, and K8s golden cases in the package.")
        else:
            actions.append(f"Address failed readiness check: {name}.")
    return actions


def _eval_ready_for_interview(eval_summary: dict[str, Any]) -> dict[str, Any]:
    """Accept full-pass evals or offline packages whose only miss is live-source proof."""
    if not eval_summary:
        return {
            "passed": False,
            "mode": "missing",
            "reason": "Eval summary is missing.",
            "failed_cases": [],
        }
    if bool(eval_summary.get("all_passed")):
        return {
            "passed": True,
            "mode": "all_passed",
            "reason": "AIOps regression eval passes completely.",
            "failed_cases": [],
        }

    failed_cases = [
        {
            "id": str(item.get("id") or ""),
            "suite": str(item.get("suite") or ""),
            "failed_metrics": [str(metric) for metric in item.get("failed_metrics") or []],
        }
        for item in eval_summary.get("failed_cases") or []
    ]
    live_source_only = bool(failed_cases) and all(
        item["suite"] == "aiops"
        and item["id"] in {"redis_maxclients_timeout", "mysql_slow_query_latency"}
        and item["failed_metrics"] == [LIVE_SOURCE_ONLY_FAILURE_METRIC]
        for item in failed_cases
    )
    if live_source_only:
        return {
            "passed": True,
            "mode": "offline_live_source_boundary",
            "reason": (
                "Offline eval is interview-ready: only Redis/MySQL live-source proof is missing, "
                "and that proof belongs to sandbox-verify or the live golden eval artifact."
            ),
            "failed_cases": failed_cases,
        }
    return {
        "passed": False,
        "mode": "non_live_eval_failures",
        "reason": "Eval has failures beyond the expected offline live-source boundary.",
        "failed_cases": failed_cases,
    }


def render_package_readme(package: dict[str, Any]) -> str:
    """Render the top-level interview package README."""
    reports = package["reports"]
    records = reports["records"]
    eval_artifacts = package.get("eval_artifacts") or {}
    eval_summary = package.get("eval_summary") or {}
    scorecard = package.get("readiness_scorecard") or {}
    output_dir = Path(package["output_dir"])

    lines = [
        "# AutoOnCall Interview Demo Package",
        "",
        "This directory is the fixed 10-minute interview path for AutoOnCall.",
        "",
        "## Narrative",
        "",
        "AutoOnCall is not a generic RAG chatbot. It is an OnCall diagnosis loop:",
        "",
        "```text",
        "Alert / Incident",
        "-> Planner",
        "-> Executor + Tool Registry",
        "-> Evidence Analyzer",
        "-> Replanner",
        "-> Report / Trace / Approval / Eval",
        "```",
        "",
        "## Demo Reports",
        "",
        "| Case | Service | Result | Evidence | Tools | Confidence | Risk | Report |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for record in records:
        report_path = Path(record["report_path"])
        result = "PASS" if record["passed"] else "FAIL"
        lines.append(
            "| "
            f"{record['id']} | "
            f"{record['service_name']} | "
            f"{result} | "
            f"{record['evidence_count']} | "
            f"{record['tool_count']} | "
            f"{record['confidence']:.2f} | "
            f"{record['risk_policy']} | "
            f"[{report_path.name}](reports/{report_path.name}) |"
        )

    lines.extend(
        [
            "",
            "## Readiness Score",
            "",
            f"- Score: `{scorecard.get('score', 0)}/{scorecard.get('max_score', READINESS_MAX_SCORE)}`",
            f"- Verdict: `{scorecard.get('verdict', 'unknown')}`",
            "",
            "| Check | Result | Weight | Reason |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for check in scorecard.get("checks", []):
        lines.append(
            "| "
            f"{check['name']} | "
            f"`{'PASS' if check['passed'] else 'CHECK'}` | "
            f"{float(check['weight']):.1f} | "
            f"{check['reason']} |"
        )
    lines.extend(
        [
            "",
            "## Eval Evidence",
            "",
        ]
    )
    if eval_artifacts:
        summary_md = _relative_artifact(output_dir, eval_artifacts.get("summary_md", ""))
        summary_json = _relative_artifact(output_dir, eval_artifacts.get("summary_json", ""))
        all_passed = eval_summary.get("all_passed")
        overall = (
            f"{eval_summary.get('overall_passed_count', 0)}/"
            f"{eval_summary.get('overall_case_count', 0)}"
        )
        lines.extend(
            [
                f"- Overall eval: {overall}; all_passed={all_passed}",
                f"- Markdown summary: [{summary_md}]({summary_md})",
                f"- JSON summary: [{summary_json}]({summary_json})",
                "- Eval is an offline deterministic regression suite, not a production RCA "
                "accuracy claim.",
            ]
        )
    else:
        lines.append("- Eval generation was skipped for this package run.")

    lines.extend(
        [
            "",
            "## Interview Order",
            "",
            "1. State the business problem: OnCall evidence is scattered and risky actions "
            "must be controlled.",
            "2. Open `reports/index.md`; present Redis/MySQL as live-adapter cases and "
            "K8s as an offline golden regression case.",
            "3. Open one report and point to tool calls, evidence matrix, confidence, and "
            "uncertainties.",
            "4. Explain that high-risk remediation is approval/dry-run/manual-record only.",
            "5. Open eval summary to show repeatable regression coverage.",
            "",
            "## Boundary",
            "",
            "- Demo reports come from offline cases and fixtures.",
            "- Redis/MySQL live proof is produced by `make interview-up`, "
            "`make sandbox-verify`, and the live golden eval command.",
            "- K8s CrashLoop/OOMKilled is offline regression coverage in the default demo.",
            "- Full-stack sandbox adapter evidence is documented separately in "
            "`deploy/sandbox.md`.",
            "- Do not describe this package as a real production deployment.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _relative_artifact(base_dir: Path, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.as_posix()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_PACKAGE_DIR))
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Demo case id to include. Repeat to control order.",
    )
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument(
        "--env-file",
        default="",
        help=(
            "Optional env file for live adapter-backed report generation. "
            "By default the package uses offline eval fixtures for stability."
        ),
    )
    parser.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Force deterministic offline fixtures even when an env file is provided.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package = asyncio.run(
        build_interview_demo_package(
            output_dir=args.output_dir,
            case_ids=args.case_ids or DEFAULT_DEMO_CASE_IDS,
            skip_eval=args.skip_eval,
            env_file=args.env_file or None,
            offline_fixtures=args.offline_fixtures,
        )
    )
    if args.json:
        print(json.dumps(package, ensure_ascii=False, indent=2))
    else:
        reports = package["reports"]
        print(
            f"Interview demo package: {reports['passed_count']}/{reports['case_count']} "
            f"reports passed; readme={package['readme']}"
        )
        if package["eval_artifacts"]:
            print(
                "Eval artifacts: "
                + ", ".join(
                    f"{name}={path}" for name, path in package["eval_artifacts"].items()
                )
            )
    return 0 if package["reports"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
