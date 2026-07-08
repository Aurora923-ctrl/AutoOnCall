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
                "conclusion_alignment": {
                    "fields": {
                        "root_cause": {
                            "aligned": True,
                            "evidence_ids": ["ev-redis"],
                            "citations": [],
                        },
                        "key_findings": [
                            {
                                "aligned": True,
                                "evidence_ids": ["ev-redis", "ev-metrics"],
                                "citations": [],
                            }
                        ],
                        "remediation_suggestion": {
                            "aligned": True,
                            "evidence_ids": ["ev-runbook"],
                            "citations": [{"source_file": "redis_postmortem.pdf"}],
                        },
                    }
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
            "case_count": 30,
            "passed_count": 30,
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
    milvus_payload = {
        "summary": {
            "status": "passed",
            "inserted_chunks": 18,
            "probe_count": 6,
            "passed_probe_count": 6,
            "pass_rate": 1.0,
            "source_counts": {
                "redis_postmortem.pdf": 1,
                "payment_wiki.html": 2,
                "tickets.xlsx": 8,
            },
            "doc_type_counts": {"pdf": 2, "html": 4, "table": 12},
        }
    }
    ragas_payload = {
        "run": {
            "metric_profile": "id-smoke",
            "answer_source": "reference-fixture",
            "judge_model": "qwen-max",
            "embedding_model": "text-embedding-v4",
            "artifacts": {"summary_md": "logs/ragas_eval_summary.md"},
        },
        "summary": {
            "status": "passed",
            "case_count": 8,
            "core_case_count": 4,
            "refusal_case_count": 2,
            "passed_count": 8,
            "pass_rate": 1.0,
            "core_case_pass_rate": 1.0,
            "id_context_precision_avg": 0.91,
            "id_context_recall_avg": 1.0,
            "oncall_actionability_avg": 1.0,
            "refusal_boundary_rate": 1.0,
            "faithfulness_avg": 0.0,
            "response_relevancy_avg": 0.0,
        },
    }

    payload = build_summary(
        live_payload=live_payload,
        rag_payload=rag_payload,
        adapter_payload=adapter_payload,
        milvus_payload=milvus_payload,
        ragas_payload=ragas_payload,
    )
    markdown = render_markdown(payload)

    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["rag_metrics"]["passed_count"] == 30
    assert payload["summary"]["ragas_quality"]["passed_count"] == 8
    assert payload["summary"]["ragas_quality"]["profile"] == "id-smoke"
    assert payload["summary"]["milvus_multisource"]["inserted_chunks"] == 18
    assert payload["summary"]["conclusion_alignment"]["aligned_count"] == 3
    assert payload["summary"]["adapter_sources"]["mock_fallback_detected"] is False
    assert "RAG eval: `30/30 passed`" in markdown
    assert "RAGAS quality: `8/8 passed`" in markdown
    assert "profile: `id-smoke`" in markdown
    assert "RAGAS id-smoke is a reproducible answer-quality regression" in markdown
    assert "refusal boundary: `100%`" in markdown
    assert "faithfulness/full judge: `not_run_in_id_smoke`" in markdown
    assert "conclusion_alignment_rate: `3/3 (100%)`" in markdown
    assert "Milvus Multi-Source Snapshot" in markdown
    assert "Probe pass rate: `6/6`" in markdown
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
    assert "logs/ragas_eval_summary.md" in readme
    assert "eval_ragas_cases.py" in readme
    assert "RAGAS" in demo_doc
    assert "logs/milvus_multisource_verification.md" in readme
    assert "--skip-rag" in demo_doc
    assert "K8s CrashLoop/OOMKilled is currently an offline golden regression case" in demo_doc
    assert "Conclusion Alignment" in redis_doc
    assert "Conclusion Alignment" in mysql_doc
    assert "Evidence Matrix" in redis_doc
    assert "Evidence Matrix" in mysql_doc
    assert "全句事实核查" in readme
