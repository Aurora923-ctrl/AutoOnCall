"""Tests for interview-facing eval summary artifacts."""

from pathlib import Path

from scripts.eval.build_interview_summary import build_summary, render_markdown

ROOT = Path(__file__).resolve().parents[1]


def test_interview_summary_rolls_up_live_aiops_rag_and_adapter_status() -> None:
    live_payload = {
        "summary": {
            "overall_case_count": 16,
            "overall_passed_count": 16,
            "overall_pass_rate": 1.0,
            "all_passed": True,
        },
        "cases": [
            {
                "id": "redis_maxclients_timeout",
                "passed": True,
                "evidence_mode": "live_adapter",
                "tool_sources": {"query_redis_status": "redis_info"},
                "metrics": {
                    "required_live_sources_hit": True,
                    "evidence_sufficiency_hit": True,
                    "runtime_vs_incident_boundary_hit": True,
                    "approval_boundary_hit": True,
                },
            },
            {
                "id": "mysql_slow_query_latency",
                "passed": True,
                "evidence_mode": "live_adapter",
                "tool_sources": {"query_mysql_status": "mysql"},
                "metrics": {
                    "required_live_sources_hit": True,
                    "evidence_sufficiency_hit": True,
                    "runtime_vs_incident_boundary_hit": True,
                    "approval_boundary_hit": True,
                },
            },
            {
                "id": "pod_crashloop",
                "passed": True,
                "evidence_mode": "offline_fixture",
                "source_boundary": "K8s is offline only",
            },
        ],
    }
    rag_payload = {
        "summary": {
            "case_count": 26,
            "passed_count": 26,
            "pass_rate": 1.0,
            "top_k": 3,
            "recall_at_k": 1.0,
            "strict_recall_at_k": 1.0,
            "mrr": 1.0,
            "citation_coverage_rate": 1.0,
            "no_answer_rejection_rate": 1.0,
            "confusion_case_pass_rate": 1.0,
        }
    }
    adapter_payload = {
        "status": "passed",
        "checks": [{"passed": True}, {"passed": True}],
        "data_sources": ["redis_info", "mysql", "prometheus", "loki"],
        "mock_fallback_detected": False,
        "missing_sources": [],
        "failed_tools": [],
    }

    payload = build_summary(
        live_payload=live_payload,
        rag_payload=rag_payload,
        adapter_payload=adapter_payload,
    )
    markdown = render_markdown(payload)

    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["rag_metrics"]["passed_count"] == 26
    assert payload["summary"]["adapter_sources"]["mock_fallback_detected"] is False
    assert "RAG eval: `26/26 passed`" in markdown
    assert "K8s CrashLoop/OOMKilled" in markdown
    assert "Conclusion alignment is conclusion-level grounding" in markdown


def test_interview_docs_keep_single_rollup_and_grounding_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    demo_doc = (ROOT / "docs" / "interview-5-minute-demo.md").read_text(encoding="utf-8")
    sandbox = (ROOT / "deploy" / "sandbox.md").read_text(encoding="utf-8")
    redis_doc = (ROOT / "docs" / "golden-chains" / "redis-maxclients.md").read_text(
        encoding="utf-8"
    )
    mysql_doc = (ROOT / "docs" / "golden-chains" / "mysql-slow-query.md").read_text(
        encoding="utf-8"
    )

    assert "logs/interview_eval_summary.md" in readme
    assert "logs/interview_eval_summary.md" in demo_doc
    assert "logs/interview_eval_summary.md" in sandbox
    assert "logs/rag_eval_summary_current.md" in readme
    assert "--skip-rag" in demo_doc
    assert "K8s CrashLoop/OOMKilled is currently an offline golden regression case" in demo_doc
    assert "Conclusion Alignment" in redis_doc
    assert "Conclusion Alignment" in mysql_doc
    assert "Evidence Matrix" in redis_doc
    assert "Evidence Matrix" in mysql_doc
    assert "全句事实核查" in readme
