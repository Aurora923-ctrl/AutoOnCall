"""Tests for shared prompt context budgeting."""

import importlib

import pytest

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.services.aiops_prompt_builder import format_raw_alert_for_prompt
from app.services.context_budget import TRUNCATION_MARKER, ContextBudget, ContextBudgeter

replanner_module = importlib.import_module("app.agent.aiops.replanner")


def test_context_budgeter_truncates_text_with_consistent_marker() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=20))

    output = budgeter.text("abcdefghijklmnopqrstuvwxyz")

    assert output.endswith(TRUNCATION_MARKER)
    assert len(output) == 20


def test_context_budgeter_serializes_json_before_truncating() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=18))

    output = budgeter.json({"b": 2, "a": 1}, sort_keys=True)

    assert output.startswith("{\n")
    assert output.endswith(TRUNCATION_MARKER)
    assert len(output) == 18


def test_context_budgeter_json_can_preserve_tail_fields() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=52))

    output = budgeter.json(
        {"first": "x" * 80, "latest": "FAILED"},
        preserve_tail=True,
    )

    assert TRUNCATION_MARKER in output
    assert "FAILED" in output


def test_context_budgeter_keeps_only_complete_ordered_sections() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=14))

    assert budgeter.sections(["first", "second", "third"]) == ["first", "second"]


def test_context_budgeter_handles_limits_shorter_than_marker() -> None:
    budgeter = ContextBudgeter()

    assert budgeter.text("abcdef", limit=3) == TRUNCATION_MARKER[:3]
    assert len(budgeter.text("abcdef", limit=3)) == 3


def test_context_budgeter_can_preserve_recent_tail_context() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=48))

    output = budgeter.text(
        ("old evidence " * 20) + "LATEST_FAILED_TOOL",
        preserve_tail=True,
    )

    assert output.endswith("TEST_FAILED_TOOL")
    assert TRUNCATION_MARKER in output
    assert len(output) == 48


def test_context_budgeter_rejects_negative_limit_and_non_string_separator() -> None:
    budgeter = ContextBudgeter()

    with pytest.raises(ValueError, match="limit"):
        budgeter.text("abcdef", limit=-1)
    with pytest.raises(TypeError, match="separator"):
        budgeter.sections(["first"], separator=None)  # type: ignore[arg-type]


def test_format_raw_alert_for_prompt_uses_shared_budget() -> None:
    budgeter = ContextBudgeter(ContextBudget(raw_alert_chars=64))

    output = format_raw_alert_for_prompt(
        {"payload": "x" * 200, "requested_action": "restart_service"},
        budgeter=budgeter,
    )

    assert output.startswith('{\n  "requested_action":')
    assert TRUNCATION_MARKER in output
    assert "restart_service" in output


def test_format_raw_alert_keeps_requested_action_and_sql_ahead_of_large_payload() -> None:
    budgeter = ContextBudgeter(ContextBudget(raw_alert_chars=4000))
    raw_alert = {
        "payload": "x" * 6000,
        "requested_action": "execute_sql",
        "sql": "DELETE FROM orders WHERE id = 42",
        "audited": False,
    }

    output = format_raw_alert_for_prompt(raw_alert, budgeter=budgeter)

    assert '"requested_action": "execute_sql"' in output
    assert '"sql": "DELETE FROM orders WHERE id = 42"' in output
    assert '"audited": false' in output
    assert TRUNCATION_MARKER in output


def test_format_raw_alert_keeps_all_priority_keys_when_their_values_are_oversized() -> None:
    budgeter = ContextBudgeter(ContextBudget(raw_alert_chars=420))
    raw_alert = {
        "requested_action": "execute_sql:" + ("x" * 800),
        "sql": "DELETE FROM orders WHERE id = 42 " + ("y" * 800),
        "audited": False,
        "reason": "operator request " + ("z" * 800),
        "payload": "p" * 4000,
    }

    output = format_raw_alert_for_prompt(raw_alert, budgeter=budgeter)

    assert len(output) <= 420
    assert '"requested_action"' in output
    assert '"sql"' in output
    assert '"audited": false' in output
    assert '"reason"' in output
    assert '"payload"' not in output
    assert TRUNCATION_MARKER in output


def test_replanner_messages_use_injected_context_budgeter() -> None:
    budgeter = ContextBudgeter(ContextBudget(truncation_marker="\n...CUT"))
    state = create_initial_aiops_state(
        "diagnose large incident context",
        session_id="context-budget-replanner",
    )
    state["incident"]["raw_alert"] = {"payload": "x" * 4000}
    analysis = EvidenceAnalysis(
        decision="add_steps",
        reason="需要补充指标和日志证据",
        missing_evidence=["query_metrics"],
    )
    analysis_decision = replanner_module.ReplanDecision(
        decision="add_steps",
        reason=analysis.reason,
    )

    messages = replanner_module._build_replanner_messages(
        state,
        analysis,
        analysis_decision,
        budgeter=budgeter,
    )

    incident_message = next(content for _, content in messages if content.startswith("Incident:"))
    assert "\n...CUT" in incident_message
    assert incident_message.endswith("}")


def test_replanner_text_preview_preserves_latest_records() -> None:
    budgeter = ContextBudgeter(ContextBudget(truncation_marker="\n...CUT"))

    output = replanner_module._text_preview(
        ("old successful record\n" * 300) + "latest failed tool record",
        limit=120,
        budgeter=budgeter,
    )

    assert output.endswith("latest failed tool record")
    assert "\n...CUT" in output


def test_replanner_evidence_preview_keeps_first_and_latest_failed_evidence() -> None:
    budgeter = ContextBudgeter(ContextBudget(truncation_marker="\n...CUT"))
    evidence = [
        {
            "source_tool": "query_metrics",
            "step_id": "first",
            "summary": "FIRST_METRIC_EVIDENCE",
            "raw_data": {"status": "success"},
        },
        *[
            {
                "source_tool": "query_logs",
                "step_id": f"middle-{index}",
                "summary": "x" * 300,
                "raw_data": {"status": "success"},
            }
            for index in range(20)
        ],
        {
            "source_tool": "query_redis_status",
            "step_id": "latest",
            "summary": "LATEST_BACKEND_FAILURE",
            "raw_data": {"status": "failed"},
        },
    ]

    output = replanner_module._text_preview(
        replanner_module._format_evidence_for_prompt(evidence),
        limit=500,
        budgeter=budgeter,
    )

    assert "FIRST_METRIC_EVIDENCE" in output
    assert "LATEST_BACKEND_FAILURE" in output
    assert "状态: failed" in output
    assert "\n...CUT" in output
