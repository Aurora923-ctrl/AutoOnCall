"""Tests for the interview Milvus multi-source verification summary."""

from __future__ import annotations

from scripts.eval.verify_milvus_multisource_rag import render_markdown, summarize


def test_milvus_multisource_summary_counts_sources_and_probe_pass_rate() -> None:
    records = [
        {
            "metadata": {
                "doc_type": "pdf",
                "source_file": "redis_postmortem.pdf",
            }
        },
        {
            "metadata": {
                "doc_type": "html",
                "source_file": "payment_wiki.html",
            }
        },
        {
            "metadata": {
                "doc_type": "table",
                "source_file": "tickets.xlsx",
            }
        },
    ]
    probes = [
        {
            "id": "redis_pdf_postmortem",
            "expected_source": "redis_postmortem.pdf",
            "passed": True,
            "retrieved": [{"source_file": "redis_postmortem.pdf"}],
        },
        {
            "id": "deploy_xlsx_history",
            "expected_source": "tickets.xlsx",
            "passed": True,
            "retrieved": [{"source_file": "tickets.xlsx"}],
        },
    ]

    payload = summarize(records, probes)
    markdown = render_markdown(payload)

    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["inserted_chunks"] == 3
    assert payload["summary"]["doc_type_counts"] == {"html": 1, "pdf": 1, "table": 1}
    assert payload["summary"]["source_counts"]["tickets.xlsx"] == 1
    assert "Milvus Multi-Source RAG Verification" in markdown
    assert "`redis_postmortem.pdf`" in markdown
    assert "Probe pass rate: `2/2`" in markdown
