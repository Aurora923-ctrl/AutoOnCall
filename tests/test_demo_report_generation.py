from pathlib import Path

import pytest

from scripts.demo.generate_demo_reports import (
    DEFAULT_DEMO_CASE_IDS,
    generate_demo_reports,
    render_index,
    safe_slug,
    select_cases,
)
from scripts.demo.run_interview_demo import build_interview_demo_package

ROOT = Path(__file__).resolve().parents[1]


def test_demo_report_defaults_match_interview_mainline_cases() -> None:
    assert DEFAULT_DEMO_CASE_IDS == (
        "redis_maxclients_timeout",
        "mysql_slow_query_latency",
        "pod_crashloop",
    )


def test_select_cases_preserves_requested_order() -> None:
    cases = [
        {"id": "mysql_slow_query_latency"},
        {"id": "redis_maxclients_timeout"},
        {"id": "pod_crashloop"},
    ]

    selected = select_cases(cases, ["redis_maxclients_timeout", "pod_crashloop"])

    assert [case["id"] for case in selected] == ["redis_maxclients_timeout", "pod_crashloop"]


def test_safe_slug_removes_path_sensitive_characters() -> None:
    assert safe_slug("../redis maxclients?") == "redis-maxclients"


@pytest.mark.asyncio
async def test_generate_demo_reports_writes_markdown_and_summary(tmp_path) -> None:
    output_dir = tmp_path / "demo_reports"
    report_db = tmp_path / "demo_reports.db"

    summary = await generate_demo_reports(
        case_ids=["redis_maxclients_timeout"],
        cases_path=ROOT / "eval" / "cases.yaml",
        output_dir=output_dir,
        report_db_path=report_db,
    )

    assert summary["case_count"] == 1
    assert summary["passed_count"] == 1
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "index.md").exists()
    assert (output_dir / "redis_maxclients_timeout.md").exists()
    assert summary["records"][0]["report_path"].endswith("redis_maxclients_timeout.md")
    assert summary["records"][0]["evidence_count"] >= 1
    assert summary["records"][0]["tool_count"] >= 1
    assert summary["records"][0]["data_sources"]
    assert summary["records"][0]["confidence_reason"]
    assert summary["records"][0]["risk_policy"] == "allow"


def test_render_index_explains_mainline_and_boundaries() -> None:
    markdown = render_index(
        {
            "case_count": 1,
            "cases_path": "eval/cases.yaml",
            "records": [
                {
                    "id": "redis_maxclients_timeout",
                    "passed": True,
                    "service_name": "order-service",
                    "evidence_count": 5,
                    "tool_count": 4,
                    "confidence": 0.8,
                    "risk_policy": "allow",
                    "report_path": "redis_maxclients_timeout.md",
                    "root_cause": "Redis maxclients exhausted",
                    "confidence_reason": "multiple supporting evidence",
                    "status": "completed",
                    "tools": ["query_redis_status", "query_metrics"],
                    "data_sources": ["redis_info", "prometheus"],
                    "evidence_profile": {
                        "by_stance": {"supporting": 4, "unknown": 1},
                        "by_data_source": {"redis_info": 1, "prometheus": 1},
                    },
                }
            ],
        }
    )

    assert "Alert / Incident -> Planner -> Executor" in markdown
    assert "Evidence Analyzer" in markdown
    assert "Boundary Statement" in markdown
    assert "production accuracy claim" in markdown
    assert "by_stance: supporting=4, unknown=1" in markdown


@pytest.mark.asyncio
async def test_build_interview_demo_package_writes_readme_and_reports(tmp_path) -> None:
    package = await build_interview_demo_package(
        output_dir=tmp_path / "interview_demo",
        case_ids=["redis_maxclients_timeout"],
        skip_eval=True,
    )

    output_dir = Path(package["output_dir"])
    assert (output_dir / "README.md").exists()
    assert (output_dir / "package_summary.json").exists()
    assert (output_dir / "reports" / "index.md").exists()
    assert (output_dir / "reports" / "redis_maxclients_timeout.md").exists()
    readme = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "AutoOnCall is not a generic RAG chatbot" in readme
    assert "Eval generation was skipped" in readme
