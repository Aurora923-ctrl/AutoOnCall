"""Tests for the offline API compatibility verifier."""

from __future__ import annotations

import json

import pytest

from scripts.eval import verify_api_contracts


@pytest.mark.asyncio
async def test_api_contract_verifier_passes_core_mainline_contracts() -> None:
    payload = await verify_api_contracts.verify_api_contracts()

    assert payload["summary"]["status"] == "passed"
    assert payload["summary"]["failed_checks"] == []

    check_ids = {item["id"] for item in payload["checks"]}
    assert {
        "chat_response",
        "chat_stream_sse",
        "aiops_sse",
        "aiops_run_status",
        "tool_contracts",
        "incident_report_schema",
        "approval_and_resume",
        "safe_change_resume",
        "eval_summary_backlog",
        "eval_ragas_quality",
    } <= check_ids

    assert payload["run"]["external_dependencies"] is False


@pytest.mark.asyncio
async def test_api_contract_verifier_writes_json_and_markdown_reports(tmp_path) -> None:
    json_path = tmp_path / "api_contract_verification.json"
    md_path = tmp_path / "api_contract_verification.md"

    payload = await verify_api_contracts.main_async(
        [
            "--summary-json",
            str(json_path),
            "--summary-md",
            str(md_path),
        ]
    )

    saved_payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")

    assert payload["summary"]["status"] == "passed"
    assert saved_payload["summary"]["passed_check_count"] == payload["summary"]["check_count"]
    assert "# AutoOnCall API Contract Verification" in markdown
    assert "`chat_response`" in markdown
    assert "`aiops_sse`" in markdown
    assert "`eval_ragas_quality`" in markdown
