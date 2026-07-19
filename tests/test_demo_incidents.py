"""Boundary tests for the central demo Incident catalog."""

from app.services.demo_incidents import build_demo_incident, canonical_demo_case_id


def test_demo_incident_id_is_trimmed_and_case_normalized() -> None:
    assert canonical_demo_case_id(" K8S-CRASHLOOP ") == "pod_crashloop"
    assert build_demo_incident(" K8S-CRASHLOOP ").incident_id == "INC-K8S-001"
