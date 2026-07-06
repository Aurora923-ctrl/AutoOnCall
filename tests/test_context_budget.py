"""Tests for shared prompt context budgeting."""

import importlib

from app.agent.aiops import create_initial_aiops_state
from app.agent.aiops.evidence_analyzer import EvidenceAnalysis
from app.services.aiops_prompt_builder import format_raw_alert_for_prompt
from app.services.context_budget import TRUNCATION_MARKER, ContextBudget, ContextBudgeter

replanner_module = importlib.import_module("app.agent.aiops.replanner")


def test_context_budgeter_truncates_text_with_consistent_marker() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=6))

    assert budgeter.text("abcdefghi") == f"abcdef{TRUNCATION_MARKER}"


def test_context_budgeter_serializes_json_before_truncating() -> None:
    budgeter = ContextBudgeter(ContextBudget(default_chars=18))

    output = budgeter.json({"b": 2, "a": 1}, sort_keys=True)

    assert output.startswith('{\n  "a": 1')
    assert output.endswith(TRUNCATION_MARKER)


def test_format_raw_alert_for_prompt_uses_shared_budget() -> None:
    budgeter = ContextBudgeter(ContextBudget(raw_alert_chars=24))

    output = format_raw_alert_for_prompt(
        {"z": "last", "a": "x" * 50},
        budgeter=budgeter,
    )

    assert output.startswith('{\n  "a":')
    assert output.endswith(TRUNCATION_MARKER)
    assert '"z":' not in output


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
    assert incident_message.endswith("\n...CUT")
