from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.sandbox.controlled_fault import (
    CommandResult,
    ControlledFaultRunner,
    ExperimentSpec,
    load_specs,
    write_run_artifacts,
)
from scripts.sandbox.controlled_fault_e2e import build_specs
from scripts.sandbox.controlled_fault_runner import parse_args

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "scripts" / "sandbox" / "controlled_fault_plan.json"


class DockerUnavailable:
    def run(self, command: list[str], *, timeout: float = 10) -> CommandResult:
        return CommandResult(command=command, returncode=127, stderr="docker daemon unavailable")


def test_plan_has_four_fault_types_with_five_runs_each() -> None:
    specs = load_specs(PLAN)

    assert len(specs) == 20
    assert {
        fault_type: sum(spec.fault_type == fault_type for spec in specs)
        for fault_type in {
            "redis_capacity",
            "mysql_slow_query",
            "downstream_http",
            "evidence_backend_outage",
        }
    } == {
        "redis_capacity": 5,
        "mysql_slow_query": 5,
        "downstream_http": 5,
        "evidence_backend_outage": 5,
    }


def test_record_schema_declares_controlled_fault_contract() -> None:
    schema = json.loads(
        (ROOT / "scripts" / "sandbox" / "controlled_fault_schema.json").read_text(encoding="utf-8")
    )

    assert schema["properties"]["schema_version"]["const"] == "controlled_fault.v1"
    assert schema["properties"]["evidence_level"]["const"] == "controlled_fault"
    assert set(schema["properties"]["fault_type"]["enum"]) == {
        "redis_capacity",
        "mysql_slow_query",
        "downstream_http",
        "evidence_backend_outage",
    }
    assert "cleanup_verification" in schema["required"]


def test_dry_run_is_default_and_never_injects() -> None:
    spec = load_specs(PLAN)[0]
    record = ControlledFaultRunner(command_runner=DockerUnavailable()).run(spec)

    assert record["status"] == "not_run"
    assert record["status_reason"] == "dry_run_plan_only"
    assert record["dry_run"] is True
    assert record["cleanup_verification"][0]["status"] == "passed"
    assert record["raw_evidence"] == []


def test_execute_requires_explicit_local_only_acknowledgement() -> None:
    spec = load_specs(PLAN)[0]
    record = ControlledFaultRunner(
        command_runner=DockerUnavailable(),
        dry_run=False,
    ).run(spec)

    assert record["status"] == "blocked"
    assert record["status_reason"] == "missing_local_only_acknowledgement"


def test_docker_unavailable_is_blocked_not_faked_as_success() -> None:
    spec = load_specs(PLAN)[0]
    record = ControlledFaultRunner(
        command_runner=DockerUnavailable(),
        dry_run=False,
        acknowledged_local_only=True,
    ).run(spec)

    assert record["status"] == "blocked"
    assert record["status_reason"].startswith("docker_unavailable:")
    assert record["diagnosis"]["status"] == "not_run"
    assert record["diagnosis"]["reason"] == "fault_injection_blocked"
    assert record["pre_checks"][0]["status"] == "blocked"


def test_production_like_and_unknown_targets_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported environment"):
        ExperimentSpec.from_dict(
            {
                "experiment_id": "bad-env",
                "fault_type": "redis_capacity",
                "target": "redis-production",
                "parameters": {"maxclients": 32, "connection_count": 24},
                "ground_truth": "redis_capacity",
                "environment": "production",
            }
        )

    spec = ExperimentSpec(
        experiment_id="bad-target",
        fault_type="redis_capacity",
        target="redis-production",
        parameters={"maxclients": 32, "connection_count": 24},
        ground_truth="redis_capacity",
    )
    record = ControlledFaultRunner(
        command_runner=DockerUnavailable(),
        dry_run=False,
        acknowledged_local_only=True,
    ).run(spec)
    assert record["status"] == "blocked"
    assert record["status_reason"].startswith("production_like_target_rejected:")


def test_parameters_are_bounded() -> None:
    with pytest.raises(ValueError, match="maxclients"):
        ExperimentSpec.from_dict(
            {
                "experiment_id": "unsafe-redis",
                "fault_type": "redis_capacity",
                "target": "autooncall-redis",
                "parameters": {"maxclients": 1, "connection_count": 4},
                "ground_truth": "redis_capacity",
            }
        )
    with pytest.raises(ValueError, match="sleep_seconds"):
        ExperimentSpec.from_dict(
            {
                "experiment_id": "unsafe-mysql",
                "fault_type": "mysql_slow_query",
                "target": "autooncall-mysql",
                "parameters": {"sleep_seconds": 60, "concurrency": 2},
                "ground_truth": "mysql_slow_query",
            }
        )


def test_loopback_downstream_fault_executes_and_cleans_up() -> None:
    spec = load_specs(PLAN)[10]
    record = ControlledFaultRunner(
        dry_run=False,
        acknowledged_local_only=True,
    ).run(spec)

    assert record["status"] == "passed"
    assert record["raw_evidence"][0]["observed_status"] == spec.parameters["status_code"]
    assert record["metrics"]["after"]["elapsed_ms"] >= spec.parameters["delay_ms"] * 0.8
    assert record["cleanup_verification"][0]["status"] == "passed"
    assert record["diagnosis"]["status"] == "not_run"
    assert record["diagnosis"]["reason"] == "diagnosis_endpoint_not_configured"


def test_summary_preserves_blocked_and_not_run_counts(tmp_path: Path) -> None:
    specs = load_specs(PLAN)[:2]
    records = [ControlledFaultRunner().run(spec) for spec in specs]
    summary = write_run_artifacts(
        output_dir=tmp_path,
        run_id="controlled-fault-test",
        records=records,
    )

    assert summary["sample_count"] == 2
    assert summary["status_counts"] == {"not_run": 2}
    assert summary["successful_injection_count"] == 0
    assert summary["blocked_or_not_run_count"] == 2
    assert summary["all_cases_have_cleanup_verification"] is True
    case = json.loads(
        (tmp_path / "controlled-fault-test" / "cases" / "cf-redis-01.json").read_text(
            encoding="utf-8"
        )
    )
    assert case["evidence_level"] == "controlled_fault"
    assert case["ground_truth"]["source"] == "experiment_label"


def test_runner_accepts_repeated_experiment_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "controlled_fault_runner.py",
            "--experiment-id",
            "cf-redis-01",
            "--experiment-id",
            "cf-mysql-01",
        ],
    )

    args = parse_args()

    assert args.experiment_id == ["cf-redis-01", "cf-mysql-01"]


def test_e2e_specs_are_bounded_and_use_original_containers() -> None:
    specs = build_specs()

    assert [(spec.fault_type, spec.target) for spec in specs] == [
        ("redis_capacity", "autooncall-redis"),
        ("mysql_slow_query", "autooncall-mysql"),
    ]
    assert specs[1].parameters["sleep_seconds"] == 3.0
    assert specs[1].parameters["concurrency"] == 8
