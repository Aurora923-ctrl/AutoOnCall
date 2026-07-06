"""Build the fixed AutoOnCall interview demo package.

The package is intentionally deterministic: it generates the three mainline
diagnosis reports and, by default, the offline eval summary that supports the
"not a one-off demo" claim.
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


async def build_interview_demo_package(
    *,
    output_dir: str | Path = DEFAULT_PACKAGE_DIR,
    case_ids: list[str] | tuple[str, ...] = DEFAULT_DEMO_CASE_IDS,
    skip_eval: bool = False,
) -> dict[str, Any]:
    """Generate reports, optional eval artifacts, and a top-level README."""
    package_dir = Path(output_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = package_dir / "reports"

    report_summary = await generate_demo_reports(
        case_ids=case_ids,
        output_dir=reports_dir,
        report_db_path=package_dir / "demo_reports.db",
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
        "reports": report_summary,
        "eval_artifacts": eval_artifacts,
        "eval_summary": eval_summary,
        "readme": str(package_dir / "README.md"),
    }
    (package_dir / "package_summary.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (package_dir / "README.md").write_text(render_package_readme(package), encoding="utf-8")
    return package


def render_package_readme(package: dict[str, Any]) -> str:
    """Render the top-level interview package README."""
    reports = package["reports"]
    records = reports["records"]
    eval_artifacts = package.get("eval_artifacts") or {}
    eval_summary = package.get("eval_summary") or {}
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
            "2. Open `reports/index.md` and show the fixed Redis/MySQL/K8s cases.",
            "3. Open one report and point to tool calls, evidence matrix, confidence, and "
            "uncertainties.",
            "4. Explain that high-risk remediation is approval/dry-run/manual-record only.",
            "5. Open eval summary to show repeatable regression coverage.",
            "",
            "## Boundary",
            "",
            "- Demo reports come from offline cases and fixtures.",
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
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package = asyncio.run(
        build_interview_demo_package(
            output_dir=args.output_dir,
            case_ids=args.case_ids or DEFAULT_DEMO_CASE_IDS,
            skip_eval=args.skip_eval,
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
