"""Table-driven tests for centralized AIOps and RAG business rules."""

import pytest

from app.services import incident_lifecycle
from app.services.incident_lifecycle import (
    incident_status_from_report_status,
    incident_status_from_runtime_status,
    manual_action_required_from_change_execution,
    report_requires_manual_action,
    snapshot_status_from_event,
    status_after_approved_run,
    status_from_change_execution,
    terminal_event_status,
)
from app.services.policies.approval_policy import (
    effective_approval_status,
    incident_status_from_approvals,
    matched_action_patterns,
)
from app.services.policies.retention_policy import (
    CHANGE_EXECUTION_TERMINAL_STATUSES,
    INCIDENT_RETENTION_TERMINAL_SQL,
    INCIDENT_RETENTION_TERMINAL_STATUSES,
    SESSION_RETENTION_ACTIVE_SQL,
    SESSION_RETENTION_ACTIVE_STATUSES,
    is_change_execution_retention_terminal,
    is_incident_retention_terminal,
    is_session_retention_active,
)
from app.services.policies.retrieval_policy import (
    is_trusted_l2_distance,
    retrieval_mode,
)


@pytest.mark.parametrize(
    ("runtime_status", "incident_status"),
    [
        ("", "diagnosing"),
        ("running", "diagnosing"),
        ("planning", "diagnosing"),
        ("executing", "diagnosing"),
        ("resume_running", "resume_running"),
        ("approval_cancelled", "approval_cancelled"),
        ("completed", "completed"),
    ],
)
def test_runtime_status_mapping(runtime_status: str, incident_status: str) -> None:
    assert incident_status_from_runtime_status(runtime_status) == incident_status


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "status"}, "running"),
        ({"type": "approval_required"}, "waiting_approval"),
        ({"type": "report"}, "completed"),
        ({"type": "report", "structured_report": {"status": "degraded"}}, "degraded"),
        ({"type": "error"}, "failed"),
    ],
)
def test_snapshot_status_mapping(event: dict[str, object], expected: str) -> None:
    assert snapshot_status_from_event(event) == expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "complete"}, "completed"),
        ({"type": "error"}, "failed"),
        ({"errors": ["boom"]}, "escalated"),
        ({"pending_approval": {"approval_id": "apr-1"}}, "waiting_approval"),
        ({"risk_assessment": {"policy": "forbidden"}}, "blocked"),
        ({"structured_report": {"status": "needs_human"}}, "needs_human"),
    ],
)
def test_terminal_event_status_mapping(event: dict[str, object], expected: str) -> None:
    assert terminal_event_status(event) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("precheck_running", "change_prechecking"),
        ("dry_run_running", "change_dry_run"),
        ("dry_run_completed", "change_validated"),
        ("sandbox_validated", "change_validated"),
        ("manual_execution_recorded", "observing"),
        ("closed", "resolved"),
        ("rollback_failed", "rollback_failed"),
        ("", "change_pending"),
    ],
)
def test_change_execution_report_status_mapping(status: str, expected: str) -> None:
    assert status_from_change_execution(status) == expected


@pytest.mark.parametrize(
    ("status", "fallback", "expected"),
    [
        ("", False, False),
        ("", True, True),
        ("closed", True, False),
        ("dry_run_completed", True, False),
        ("waiting_manual_execution", False, True),
        ("rollback_failed", False, True),
    ],
)
def test_change_execution_manual_action_rule(
    status: str,
    fallback: bool,
    expected: bool,
) -> None:
    assert manual_action_required_from_change_execution(status, fallback=fallback) is expected


@pytest.mark.parametrize(
    ("statuses", "latest", "incident_status", "approval_status"),
    [
        ([], "", "investigating", "not_required"),
        (["approved"], "approved", "approval_approved", "approved"),
        (["rejected", "pending"], "pending", "waiting_approval", "pending"),
        (["cancelled"], "cancelled", "approval_cancelled", "cancelled"),
    ],
)
def test_approval_status_precedence(
    statuses: list[str],
    latest: str,
    incident_status: str,
    approval_status: str,
) -> None:
    assert incident_status_from_approvals(statuses) == incident_status
    assert effective_approval_status(statuses, latest_status=latest) == approval_status


@pytest.mark.parametrize(
    ("context", "forbidden", "expected_rule"),
    [
        ("run rm -rf /data", True, "shell:rm-rf"),
        ("DELETE FROM orders", True, "sql:delete"),
        ("restart service order-api", False, "action:restart"),
        ("\u8c03\u6574 Redis maxclients", False, "action:redis-config"),
    ],
)
def test_risk_action_pattern_rules(
    context: str,
    forbidden: bool,
    expected_rule: str,
) -> None:
    assert expected_rule in matched_action_patterns(context, forbidden=forbidden)


def test_retention_status_classification_and_sql_literals_stay_aligned() -> None:
    assert all(is_change_execution_retention_terminal(status) for status in CHANGE_EXECUTION_TERMINAL_STATUSES)
    assert all(is_session_retention_active(status) for status in SESSION_RETENTION_ACTIVE_STATUSES)
    assert all(is_incident_retention_terminal(status) for status in INCIDENT_RETENTION_TERMINAL_STATUSES)
    assert all(f"'{status}'" in SESSION_RETENTION_ACTIVE_SQL for status in SESSION_RETENTION_ACTIVE_STATUSES)
    assert all(
        f"'{status}'" in INCIDENT_RETENTION_TERMINAL_SQL
        for status in INCIDENT_RETENTION_TERMINAL_STATUSES
    )


@pytest.mark.parametrize(
    ("hybrid", "rerank", "strategy", "degraded", "expected"),
    [
        (False, False, "weighted", "", "vector"),
        (True, True, "rrf", "", "hybrid_vector_lexical_rrf_rerank"),
        (True, False, "weighted", "vector", "lexical_degraded"),
        (True, True, "weighted", "lexical", "vector_degraded_rerank"),
    ],
)
def test_retrieval_mode_matrix(
    hybrid: bool,
    rerank: bool,
    strategy: str,
    degraded: str,
    expected: str,
) -> None:
    assert (
        retrieval_mode(
            hybrid_enabled=hybrid,
            rerank_enabled=rerank,
            fusion_strategy=strategy,
            degraded_backend=degraded,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("score", "threshold", "trusted"),
    [(None, 1.0, False), ("bad", 1.0, False), (-0.1, 1.0, False), (0.8, 1.0, True)],
)
def test_retrieval_vector_trust_gate(score: object, threshold: float, trusted: bool) -> None:
    assert is_trusted_l2_distance(score, threshold) is trusted


def test_report_manual_action_and_post_approval_rules() -> None:
    assert report_requires_manual_action("waiting_approval") is True
    assert report_requires_manual_action("completed") is False
    assert report_requires_manual_action("completed", risk_forbidden=True) is True
    assert incident_status_from_report_status("") == "completed"
    assert status_after_approved_run("closed") == "closed"
    assert status_after_approved_run("completed") == "approval_approved"


def test_incident_lifecycle_keeps_compatibility_exports() -> None:
    assert incident_lifecycle.incident_status_from_runtime_status is incident_status_from_runtime_status
    assert incident_lifecycle.status_from_change_execution is status_from_change_execution


def test_production_environment_normalizes_unicode_width() -> None:
    assert incident_lifecycle.is_production_environment("ＰＲＯＤ-cn") is True
