"""Focused tests for stage-four AIOps orchestration boundaries."""

from app.agent.aiops.evidence_recommendations import (
    build_recommended_steps,
    build_retry_steps,
)
from app.agent.aiops.fallback_scenarios import match_fallback_scenario
from app.agent.aiops.replan_decision import ReplanDecision, normalize_llm_replan_decision
from app.agent.aiops.state import PlanExecuteState
from app.models.plan import PlanStep


def test_fallback_scenario_table_matches_core_incident_families() -> None:
    assert match_fallback_scenario("Redis maxclients timeout").name == "redis"
    assert match_fallback_scenario("MySQL slow query").name == "mysql"
    assert match_fallback_scenario("Pod CrashLoopBackOff").name == "crashloop"


def test_evidence_recommendations_build_read_only_steps_and_one_retry() -> None:
    steps = build_recommended_steps(["query_metrics", "query_redis_status"], "order-service")
    retries = build_retry_steps(
        [
            {
                "step_id": "s1",
                "tool_name": "query_logs",
                "input_args": {"service_name": "order-service"},
            },
            {
                "step_id": "s2",
                "tool_name": "query_metrics",
                "input_args": {"service_name": "order-service"},
            },
        ]
    )

    assert [step.tool_name for step in steps] == ["query_metrics", "query_redis_status"]
    assert all(step.risk_level == "low" for step in steps)
    assert len(retries) == 1
    assert retries[0].step_id == "s1-retry"


def test_replan_decision_rejects_unsafe_llm_steps() -> None:
    state = PlanExecuteState(
        current_plan=[],
        plan=[],
        incident={"incident_id": "inc-stage4", "environment": "prod"},
    )
    analysis = type(
        "Analysis",
        (),
        {"evidence_sufficient": False},
    )()
    decision = ReplanDecision(
        decision="add_steps",
        new_steps=[
            PlanStep(
                step_id="unsafe",
                tool_name="delete_pod",
                risk_level="high",
            )
        ],
    )

    assert (
        normalize_llm_replan_decision(
            decision,
            state,
            analysis,
            ReplanDecision(decision="add_steps"),
        )
        is None
    )
