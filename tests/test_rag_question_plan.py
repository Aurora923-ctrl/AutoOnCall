from app.services.rag_question_plan import build_question_plan, entities_for_subgoal


def test_builds_mysql_evidence_and_diagnosis_plan_for_slow_query_investigation() -> None:
    plan = build_question_plan(
        "payment-service 的 pool_waiting 和 active_connections 上升，如何排查慢查询？"
    )

    assert plan.domain == "mysql"
    assert set(plan.explicit_entities) >= {
        "payment-service",
        "pool_waiting",
        "active_connections",
        "慢查询",
        "EXPLAIN",
    }
    assert {subgoal.intent for subgoal in plan.subgoals} == {"evidence", "diagnosis"}
    assert all(not subgoal.action_requested for subgoal in plan.subgoals)
    assert entities_for_subgoal(plan, "diagnosis") == (
        "payment-service",
        "pool_waiting",
        "active_connections",
        "慢查询",
        "EXPLAIN",
    )
    assert plan.max_claims == 3


def test_builds_redis_official_and_postmortem_capacity_plan() -> None:
    plan = build_question_plan(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？"
    )

    assert {role for subgoal in plan.subgoals for role in subgoal.required_source_roles} >= {
        "official",
        "postmortem",
    }
    assert set(plan.explicit_entities) >= {
        "connected_clients",
        "maxclients",
        "effective_capacity",
        "blocked_clients",
    }
    assert plan.max_claims == 5


def test_adds_action_subgoal_only_for_explicit_production_action() -> None:
    plan = build_question_plan("发布后 pool_waiting 上升，如何判断是否回滚？")

    assert any(
        subgoal.intent == "action" and subgoal.action_requested for subgoal in plan.subgoals
    )
