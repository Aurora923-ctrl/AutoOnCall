"""Tests for durable AIOps session snapshots."""

from app.models.aiops_session import AIOpsSessionSnapshot
from app.models.incident_state import IncidentState
from app.services.sqlite_store import AIOpsSQLiteStore


def test_sqlite_store_upserts_aiops_session_snapshot(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")

    first = AIOpsSessionSnapshot.from_state(
        session_id="session-redis",
        status="running",
        node_name="planner",
        state={
            "input": "诊断 Redis timeout",
            "trace_id": "trace-redis",
            "incident": {
                "incident_id": "inc-redis",
                "service_name": "order-service",
                "severity": "P1",
            },
            "current_plan": [
                {
                    "step_id": "step-1",
                    "tool_name": "query_redis_status",
                    "purpose": "检查 Redis 连接数",
                }
            ],
        },
    )
    store.save_aiops_session_snapshot(first)

    saved = store.get_aiops_session_snapshot("session-redis")
    assert saved is not None
    assert saved.session_id == "session-redis"
    assert saved.incident_id == "inc-redis"
    assert saved.trace_id == "trace-redis"
    assert saved.status == "running"
    assert saved.current_plan[0]["tool_name"] == "query_redis_status"
    state = saved.to_state()
    assert state["session_id"] == "session-redis"
    assert state["incident"]["incident_id"] == "inc-redis"

    second = AIOpsSessionSnapshot.from_state(
        session_id="session-redis",
        status="waiting_approval",
        node_name="replanner",
        state={
            "trace_id": "trace-redis",
            "incident": {"incident_id": "inc-redis"},
            "pending_approval": {
                "approval_id": "apr-redis",
                "action": "调整 Redis maxclients",
                "status": "pending",
            },
            "risk_assessment": {
                "policy": "approval_required",
                "risk_level": "medium",
            },
            "report": {"report_id": "report-redis", "status": "waiting_approval"},
        },
    )
    store.save_aiops_session_snapshot(second)

    updated = store.get_aiops_session_snapshot("session-redis")
    assert updated is not None
    assert updated.status == "waiting_approval"
    assert updated.node_name == "replanner"
    assert updated.pending_approval["approval_id"] == "apr-redis"
    assert updated.risk_assessment["policy"] == "approval_required"
    assert updated.final_report_id == "report-redis"
    assert updated.created_at == saved.created_at
    assert updated.updated_at >= saved.updated_at

    latest = store.get_latest_aiops_session_snapshot("inc-redis")
    assert latest is not None
    assert latest.session_id == "session-redis"

    other = AIOpsSessionSnapshot.from_state(
        session_id="session-mysql",
        status="completed",
        node_name="workflow",
        state={
            "input": "诊断 MySQL slow query",
            "trace_id": "trace-mysql",
            "incident": {
                "incident_id": "inc-mysql",
                "service_name": "payment-service",
                "severity": "P2",
            },
        },
    )
    store.save_aiops_session_snapshot(other)

    all_snapshots = store.list_aiops_session_snapshots(limit=10)
    assert [snapshot.session_id for snapshot in all_snapshots] == [
        "session-mysql",
        "session-redis",
    ]
    redis_snapshots = store.list_aiops_session_snapshots(incident_id="inc-redis", limit=10)
    assert [snapshot.session_id for snapshot in redis_snapshots] == ["session-redis"]
    assert store.list_aiops_session_snapshots(incident_id="missing", limit=10) == []


def test_sqlite_store_returns_none_for_missing_aiops_session_snapshot(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")

    assert store.get_aiops_session_snapshot("missing-session") is None


def test_aiops_session_snapshot_preserves_string_hypotheses(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    snapshot = AIOpsSessionSnapshot.from_state(
        session_id="session-hypothesis",
        state={
            "trace_id": "trace-hypothesis",
            "incident": {"incident_id": "inc-hypothesis"},
            "hypotheses": [
                "Redis maxclients 接近上限导致连接被拒绝",
                {"value": "应用连接池重试放大依赖压力"},
            ],
        },
    )

    store.save_aiops_session_snapshot(snapshot)

    saved = store.get_aiops_session_snapshot("session-hypothesis")
    assert saved is not None
    assert saved.hypotheses == [
        "Redis maxclients 接近上限导致连接被拒绝",
        "应用连接池重试放大依赖压力",
    ]
    assert saved.to_state()["hypotheses"] == saved.hypotheses


def test_sqlite_store_upserts_incident_state_without_losing_identity_fields(tmp_path) -> None:
    store = AIOpsSQLiteStore(tmp_path / "aiops.db")
    first = IncidentState(
        incident_id="inc-state",
        status="diagnosing",
        title="order-service Redis timeout",
        service_name="order-service",
        severity="P1",
        environment="prod",
        summary="Redis timeout",
        trace_id="trace-state",
        session_id="session-state",
    )
    store.save_incident_state(first)

    store.save_incident_state(
        IncidentState(
            incident_id="inc-state",
            status="waiting_approval",
            status_reason="Approval request created",
            approval_status="pending",
            latest_approval_id="apr-state",
            manual_action_required=True,
            metadata={"source": "approval"},
        )
    )

    updated = store.get_incident_state("inc-state")
    assert updated is not None
    assert updated.status == "waiting_approval"
    assert updated.title == "order-service Redis timeout"
    assert updated.service_name == "order-service"
    assert updated.severity == "P1"
    assert updated.environment == "prod"
    assert updated.trace_id == "trace-state"
    assert updated.session_id == "session-state"
    assert updated.latest_approval_id == "apr-state"
    assert updated.manual_action_required is True
    assert updated.created_at == first.created_at
    assert store.list_incident_states()[0].incident_id == "inc-state"
