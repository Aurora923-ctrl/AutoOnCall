"""Bounded, auditable controlled-fault experiments for the local sandbox."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

ALLOWED_ENVIRONMENTS = {"local", "sandbox"}
FORBIDDEN_TARGET_MARKERS = {"prod", "production", "prd"}
ALLOWED_CONTAINERS = {
    "redis_capacity": {
        "autooncall-redis": ("autooncall", "redis"),
    },
    "mysql_slow_query": {
        "autooncall-mysql": ("autooncall", "mysql"),
    },
    "evidence_backend_outage": {
        "autooncall-prometheus": ("autooncall", "prometheus"),
        "autooncall-loki": ("autooncall", "loki"),
    },
}
FAULT_TYPES = {
    "redis_capacity",
    "mysql_slow_query",
    "downstream_http",
    "evidence_backend_outage",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    fault_type: str
    target: str
    parameters: dict[str, Any]
    ground_truth: str
    environment: str = "sandbox"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExperimentSpec:
        spec = cls(
            experiment_id=str(payload["experiment_id"]),
            fault_type=str(payload["fault_type"]),
            target=str(payload["target"]),
            parameters=dict(payload.get("parameters") or {}),
            ground_truth=str(payload["ground_truth"]),
            environment=str(payload.get("environment") or "sandbox"),
        )
        validate_spec(spec)
        return spec


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def as_evidence(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
        }


@dataclass
class FaultContext:
    recovery: Callable[[], list[dict[str, Any]]]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    before_metrics: dict[str, Any] = field(default_factory=dict)
    after_metrics: dict[str, Any] = field(default_factory=dict)


class BlockedExperiment(RuntimeError):
    """Raised when a required local dependency is unavailable."""


class CommandRunner:
    def run(self, command: list[str], *, timeout: float = 10) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return CommandResult(command=command, returncode=127, stderr=str(exc))
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
        )


class ControlledFaultRunner:
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        dry_run: bool = True,
        acknowledged_local_only: bool = False,
        diagnosis_url: str = "",
        diagnosis_runner: Callable[[ExperimentSpec], dict[str, Any]] | None = None,
    ) -> None:
        self.commands = command_runner or CommandRunner()
        self.dry_run = dry_run
        self.acknowledged_local_only = acknowledged_local_only
        self.diagnosis_url = diagnosis_url
        self.diagnosis_runner = diagnosis_runner

    def run(self, spec: ExperimentSpec) -> dict[str, Any]:
        validate_spec(spec)
        started_at = utc_now()
        record = _base_record(spec, started_at, self.dry_run)
        if self.dry_run:
            record["status"] = "not_run"
            record["status_reason"] = "dry_run_plan_only"
            record["pre_checks"].append(
                {"name": "environment_guard", "status": "passed", "environment": spec.environment}
            )
            record["cleanup_verification"].append(
                {"name": "no_injection_performed", "status": "passed"}
            )
            record["ended_at"] = utc_now()
            return record
        if not self.acknowledged_local_only:
            record["status"] = "blocked"
            record["status_reason"] = "missing_local_only_acknowledgement"
            record["pre_checks"].append({"name": "environment_guard", "status": "blocked"})
            record["cleanup_verification"].append(
                {"name": "no_injection_performed", "status": "passed"}
            )
            record["ended_at"] = utc_now()
            return record

        context: FaultContext | None = None
        try:
            record["pre_checks"].append(self._environment_precheck(spec))
            context = self._inject(spec)
            record["injection_started_at"] = utc_now()
            record["raw_evidence"].extend(context.evidence)
            record["metrics"]["before"] = context.before_metrics
            record["metrics"]["after"] = context.after_metrics
            record["status"] = "injected"
            hold_seconds = float(spec.parameters.get("hold_seconds", 0.2))
            time.sleep(min(max(hold_seconds, 0.0), 5.0))
            self._record_diagnosis(record)
            record["injection_ended_at"] = utc_now()
        except BlockedExperiment as exc:
            record["status"] = "blocked"
            record["status_reason"] = str(exc)
            record["pre_checks"].append(
                {
                    "name": "dependency_availability",
                    "status": "blocked",
                    "reason": str(exc),
                }
            )
            record["diagnosis"]["reason"] = "fault_injection_blocked"
        except Exception as exc:
            record["status"] = "failed"
            record["status_reason"] = f"{type(exc).__name__}: {exc}"
        finally:
            if context is not None:
                try:
                    record["recovery_started_at"] = utc_now()
                    cleanup = context.recovery()
                    record["cleanup_verification"].extend(cleanup)
                    record["recovery_ended_at"] = utc_now()
                    if all(item.get("status") == "passed" for item in cleanup):
                        if record["status"] == "injected":
                            record["status"] = "passed"
                    else:
                        record["status"] = "failed"
                        record["status_reason"] = "cleanup_verification_failed"
                except Exception as exc:
                    record["status"] = "failed"
                    record["status_reason"] = f"recovery_failed: {type(exc).__name__}: {exc}"
            else:
                record["cleanup_verification"].append(
                    {"name": "no_injection_or_partial_state", "status": "passed"}
                )
            record["ended_at"] = utc_now()
        return record

    def _environment_precheck(self, spec: ExperimentSpec) -> dict[str, Any]:
        if spec.environment not in ALLOWED_ENVIRONMENTS:
            raise BlockedExperiment(f"environment_not_allowed:{spec.environment}")
        lowered_target = spec.target.lower()
        if any(marker in lowered_target for marker in FORBIDDEN_TARGET_MARKERS):
            raise BlockedExperiment(f"production_like_target_rejected:{spec.target}")
        if spec.fault_type == "downstream_http":
            if spec.target not in {"127.0.0.1", "localhost"}:
                raise BlockedExperiment(f"non_loopback_target_rejected:{spec.target}")
            return {"name": "environment_guard", "status": "passed", "target": spec.target}

        expected_identity = ALLOWED_CONTAINERS[spec.fault_type].get(spec.target)
        if expected_identity is None:
            raise BlockedExperiment(f"container_not_allowlisted:{spec.target}")
        docker = self.commands.run(["docker", "info", "--format", "{{.ServerVersion}}"])
        if docker.returncode != 0:
            raise BlockedExperiment(f"docker_unavailable:{docker.stderr or docker.stdout}")
        inspect = self.commands.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}|'
                "{{.State.Running}}",
                spec.target,
            ]
        )
        project, service = expected_identity
        expected_output = f"{project}|{service}|true"
        if inspect.returncode != 0 or inspect.stdout.strip() != expected_output:
            raise BlockedExperiment(
                f"sandbox_container_identity_failed:{inspect.stderr or inspect.stdout}"
            )
        return {
            "name": "environment_guard",
            "status": "passed",
            "target": spec.target,
            "compose_project": project,
            "compose_service": service,
        }

    def _inject(self, spec: ExperimentSpec) -> FaultContext:
        injectors = {
            "redis_capacity": self._inject_redis,
            "mysql_slow_query": self._inject_mysql,
            "downstream_http": self._inject_downstream,
            "evidence_backend_outage": self._inject_evidence_outage,
        }
        return injectors[spec.fault_type](spec)

    def _inject_redis(self, spec: ExperimentSpec) -> FaultContext:
        before = self._docker_exec(spec.target, ["redis-cli", "CONFIG", "GET", "maxclients"])
        original = _last_int(before.stdout)
        maxclients = int(spec.parameters["maxclients"])
        connection_count = int(spec.parameters["connection_count"])
        changed = self._docker_exec(
            spec.target, ["redis-cli", "CONFIG", "SET", "maxclients", str(maxclients)]
        )
        if changed.returncode != 0 or "OK" not in changed.stdout:
            raise RuntimeError(f"redis injection failed: {changed.stderr or changed.stdout}")
        held_connections: list[socket.socket] = []
        connection_errors: list[str] = []
        for _ in range(connection_count):
            client = socket.socket()
            client.settimeout(1)
            try:
                client.connect(("127.0.0.1", 16379))
                client.sendall(b"*1\r\n$4\r\nPING\r\n")
                response = client.recv(128)
                if b"PONG" not in response:
                    raise RuntimeError(f"unexpected Redis response: {response!r}")
                held_connections.append(client)
            except Exception as exc:
                client.close()
                connection_errors.append(str(exc))
        during = self._docker_exec(spec.target, ["redis-cli", "INFO", "clients"])

        def recover() -> list[dict[str, Any]]:
            for client in held_connections:
                client.close()
            restored = self._docker_exec(
                spec.target, ["redis-cli", "CONFIG", "SET", "maxclients", str(original)]
            )
            verified = self._docker_exec(spec.target, ["redis-cli", "CONFIG", "GET", "maxclients"])
            return [
                {
                    "name": "redis_maxclients_restored",
                    "status": (
                        "passed"
                        if restored.returncode == 0 and _last_int(verified.stdout) == original
                        else "failed"
                    ),
                    "expected": original,
                    "actual": _last_int(verified.stdout),
                    "evidence": [restored.as_evidence(), verified.as_evidence()],
                }
            ]

        return FaultContext(
            recovery=recover,
            evidence=[
                before.as_evidence(),
                changed.as_evidence(),
                {
                    "source": "bounded_redis_connections",
                    "requested": connection_count,
                    "opened": len(held_connections),
                    "errors": connection_errors,
                },
                during.as_evidence(),
            ],
            before_metrics={"maxclients": original},
            after_metrics={
                "injected_maxclients": maxclients,
                "held_connections": len(held_connections),
                "capacity_ratio": round(len(held_connections) / maxclients, 4),
            },
        )

    def _inject_mysql(self, spec: ExperimentSpec) -> FaultContext:
        seconds = float(spec.parameters["sleep_seconds"])
        concurrency = int(spec.parameters["concurrency"])
        mysql_user = os.getenv("AUTOONCALL_MYSQL_USER", "autooncall")
        mysql_password = os.getenv("AUTOONCALL_MYSQL_PASSWORD", "autooncall123")
        query = f"SELECT SLEEP({seconds:.3f}) AS controlled_fault_sleep;"
        before = self._docker_exec(
            spec.target,
            [
                "mysql",
                f"-u{mysql_user}",
                f"-p{mysql_password}",
                "-e",
                "SHOW STATUS LIKE 'Threads_connected';",
            ],
        )
        command_args = ["mysql", f"-u{mysql_user}", f"-p{mysql_password}", "-e", query]
        executor = ThreadPoolExecutor(max_workers=concurrency)
        started = time.perf_counter()
        futures = [
            executor.submit(
                self._docker_exec,
                spec.target,
                command_args,
                timeout=seconds + 5,
            )
            for _index in range(concurrency)
        ]
        time.sleep(min(0.05, seconds / 2))

        def recover() -> list[dict[str, Any]]:
            injected_results = [future.result(timeout=seconds + 5) for future in futures]
            executor.shutdown(wait=True)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            failed = [result for result in injected_results if result.returncode != 0]
            ping = self._docker_exec(
                spec.target,
                ["mysqladmin", f"-u{mysql_user}", f"-p{mysql_password}", "ping"],
            )
            return [
                {
                    "name": "mysql_health_after_bounded_query",
                    "status": (
                        "passed" if not failed and "mysqld is alive" in ping.stdout else "failed"
                    ),
                    "query_elapsed_ms": elapsed_ms,
                    "evidence": [
                        *[result.as_evidence() for result in injected_results],
                        ping.as_evidence(),
                    ],
                }
            ]

        return FaultContext(
            recovery=recover,
            evidence=[
                before.as_evidence(),
                {
                    "source": "bounded_mysql_concurrency",
                    "concurrency": concurrency,
                    "query": query,
                    "status": "running_during_diagnosis",
                },
            ],
            before_metrics={"threads_connected_raw": before.stdout},
            after_metrics={
                "sleep_seconds": seconds,
                "concurrency": concurrency,
                "queries_started": len(futures),
            },
        )

    def _inject_downstream(self, spec: ExperimentSpec) -> FaultContext:
        delay_ms = int(spec.parameters["delay_ms"])
        status_code = int(spec.parameters["status_code"])
        server = _start_fault_server(delay_ms=delay_ms, status_code=status_code)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.perf_counter()
        observed_status = 0
        try:
            with urlopen(f"http://127.0.0.1:{port}/controlled-fault", timeout=5) as response:
                observed_status = response.status
        except Exception as exc:
            observed_status = int(getattr(exc, "code", 0))
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        def recover() -> list[dict[str, Any]]:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            closed = not _port_is_open("127.0.0.1", port)
            return [
                {
                    "name": "downstream_fault_server_closed",
                    "status": "passed" if closed else "failed",
                    "port": port,
                }
            ]

        return FaultContext(
            recovery=recover,
            evidence=[
                {
                    "source": "loopback_http_probe",
                    "url": f"http://127.0.0.1:{port}/controlled-fault",
                    "observed_status": observed_status,
                    "elapsed_ms": elapsed_ms,
                }
            ],
            before_metrics={"listener_present": False},
            after_metrics={
                "delay_ms": delay_ms,
                "expected_status": status_code,
                "observed_status": observed_status,
                "elapsed_ms": elapsed_ms,
            },
        )

    def _inject_evidence_outage(self, spec: ExperimentSpec) -> FaultContext:
        stopped = self.commands.run(["docker", "stop", "--time", "3", spec.target], timeout=8)
        if stopped.returncode != 0:
            raise RuntimeError(f"container stop failed: {stopped.stderr or stopped.stdout}")

        def recover() -> list[dict[str, Any]]:
            started = self.commands.run(["docker", "start", spec.target], timeout=15)
            inspect = self.commands.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", spec.target]
            )
            return [
                {
                    "name": "evidence_backend_restarted",
                    "status": (
                        "passed"
                        if started.returncode == 0 and inspect.stdout.strip().lower() == "true"
                        else "failed"
                    ),
                    "evidence": [started.as_evidence(), inspect.as_evidence()],
                }
            ]

        return FaultContext(
            recovery=recover,
            evidence=[stopped.as_evidence()],
            before_metrics={"container_running": True},
            after_metrics={"container_running": False},
        )

    def _docker_exec(
        self, container: str, args: list[str], *, timeout: float = 10
    ) -> CommandResult:
        return self.commands.run(["docker", "exec", container, *args], timeout=timeout)

    def _record_diagnosis(self, record: dict[str, Any]) -> None:
        if self.diagnosis_runner is not None:
            record["alert_at"] = record.get("injection_started_at") or utc_now()
            record["diagnosis_started_at"] = utc_now()
            result = self.diagnosis_runner(
                ExperimentSpec(
                    experiment_id=str(record["experiment_id"]),
                    fault_type=str(record["fault_type"]),
                    target=str(record["target"]),
                    parameters=dict(record["injection_parameters"]),
                    ground_truth=str(record["ground_truth"]["root_cause"]),
                    environment=str(record["environment"]),
                )
            )
            record["first_useful_diagnosis_at"] = utc_now()
            record["diagnosis"].update(result)
            record["diagnosis"]["status"] = "passed" if result.get("top_1_rca") else "failed"
            return
        if not self.diagnosis_url:
            record["diagnosis"]["status"] = "not_run"
            record["diagnosis"]["reason"] = "diagnosis_endpoint_not_configured"
            return
        record["diagnosis"]["status"] = "not_run"
        record["diagnosis"]["reason"] = "diagnosis_endpoint_integration_not_implemented"


def validate_spec(spec: ExperimentSpec) -> None:
    if spec.fault_type not in FAULT_TYPES:
        raise ValueError(f"unsupported fault_type:{spec.fault_type}")
    if spec.environment not in ALLOWED_ENVIRONMENTS:
        raise ValueError(f"unsupported environment:{spec.environment}")
    if not spec.experiment_id or not spec.ground_truth:
        raise ValueError("experiment_id and ground_truth are required")
    params = spec.parameters
    if spec.fault_type == "redis_capacity":
        maxclients = _bounded_int(params, "maxclients", 16, 64)
        connection_count = _bounded_int(params, "connection_count", 4, 60)
        if connection_count > maxclients - 2:
            raise ValueError("connection_count must leave at least two recovery connections")
    elif spec.fault_type == "mysql_slow_query":
        _bounded_float(params, "sleep_seconds", 0.05, 3.0)
        _bounded_int(params, "concurrency", 2, 8)
    elif spec.fault_type == "downstream_http":
        _bounded_int(params, "delay_ms", 25, 2000)
        status = _bounded_int(params, "status_code", 200, 599)
        if status not in {200, 429, 500, 502, 503, 504}:
            raise ValueError("status_code is not in the controlled allowlist")
    elif spec.fault_type == "evidence_backend_outage":
        _bounded_float(params, "hold_seconds", 0.05, 5.0)
    if "hold_seconds" in params:
        _bounded_float(params, "hold_seconds", 0.0, 5.0)


def load_specs(path: Path) -> list[ExperimentSpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    specs = [ExperimentSpec.from_dict(item) for item in payload["experiments"]]
    if len(specs) != 20:
        raise ValueError(
            f"controlled-fault plan must contain exactly 20 experiments, got {len(specs)}"
        )
    counts = dict.fromkeys(FAULT_TYPES, 0)
    for spec in specs:
        counts[spec.fault_type] += 1
    if set(counts.values()) != {5}:
        raise ValueError(f"each fault type must appear exactly five times: {counts}")
    return specs


def write_run_artifacts(
    *,
    output_dir: Path,
    run_id: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    run_dir = output_dir / run_id
    cases_dir = run_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=False)
    for record in records:
        path = cases_dir / f"{record['experiment_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    statuses: dict[str, int] = {}
    for record in records:
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
    summary = {
        "schema_version": "controlled_fault.v1",
        "run_id": run_id,
        "evidence_level": "controlled_fault",
        "sample_count": len(records),
        "status_counts": statuses,
        "successful_injection_count": statuses.get("passed", 0),
        "blocked_or_not_run_count": statuses.get("blocked", 0) + statuses.get("not_run", 0),
        "diagnosis_status_counts": _count_nested_status(records, "diagnosis"),
        "fault_type_status_counts": _fault_type_status_counts(records),
        "all_cases_have_cleanup_verification": all(
            bool(record["cleanup_verification"]) for record in records
        ),
        "case_artifact_dir": str(cases_dir),
        "claim_boundary": (
            "Only status=passed records represent executed and recovered local faults. "
            "blocked/not_run records are audit attempts, not successful experiments."
        ),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _base_record(spec: ExperimentSpec, started_at: str, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": "controlled_fault.v1",
        "experiment_id": spec.experiment_id,
        "evidence_level": "controlled_fault",
        "environment": spec.environment,
        "dry_run": dry_run,
        "fault_type": spec.fault_type,
        "target": spec.target,
        "injection_parameters": spec.parameters,
        "ground_truth": {
            "source": "experiment_label",
            "root_cause": spec.ground_truth,
        },
        "status": "planned",
        "status_reason": "",
        "started_at": started_at,
        "ended_at": None,
        "injection_started_at": None,
        "injection_ended_at": None,
        "alert_at": None,
        "diagnosis_started_at": None,
        "first_useful_diagnosis_at": None,
        "recovery_started_at": None,
        "recovery_ended_at": None,
        "diagnosis": {
            "status": "not_run",
            "reason": "",
            "top_1_rca": None,
            "top_3_rca": [],
            "tools": [],
            "data_sources": [],
            "evidence_completeness": None,
            "replan_triggered": None,
            "needs_human": None,
            "report_status": "not_run",
        },
        "metrics": {"before": {}, "after": {}},
        "pre_checks": [],
        "raw_evidence": [],
        "cleanup_verification": [],
    }


def _bounded_int(params: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = int(params[key])
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _bounded_float(params: dict[str, Any], key: str, minimum: float, maximum: float) -> float:
    value = float(params[key])
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _last_int(value: str) -> int:
    for line in reversed(value.splitlines()):
        try:
            return int(line.strip())
        except ValueError:
            continue
    raise ValueError(f"integer not found in output: {value!r}")


def _start_fault_server(*, delay_ms: int, status_code: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            time.sleep(delay_ms / 1000)
            body = json.dumps({"fault": "controlled_downstream", "status": status_code}).encode(
                "utf-8"
            )
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((host, port)) == 0


def _count_nested_status(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key, {}).get("status") or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _fault_type_status_counts(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        fault_type = str(record["fault_type"])
        status = str(record["status"])
        fault_counts = counts.setdefault(fault_type, {})
        fault_counts[status] = fault_counts.get(status, 0) + 1
    return counts
