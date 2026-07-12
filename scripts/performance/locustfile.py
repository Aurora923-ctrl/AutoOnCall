"""Locust scenarios for deterministic and explicitly enabled real-model load tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

from locust import HttpUser, between, events, task

LOAD_PROFILE = os.getenv("AUTOONCALL_LOAD_PROFILE", "fixture").strip().lower()
EVIDENCE_LEVEL = os.getenv("AUTOONCALL_EVIDENCE_LEVEL", "offline_fixture").strip().lower()
ENABLE_MODEL_REQUESTS = os.getenv("AUTOONCALL_ENABLE_MODEL_REQUESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
RUN_ID = os.getenv("AUTOONCALL_LOAD_RUN_ID", f"locust-{uuid4().hex[:12]}")
ARTIFACT_DIR = os.getenv("AUTOONCALL_LOAD_ARTIFACT_DIR", "logs")
VALID_PROFILES = {"fixture", "real_model"}
VALID_EVIDENCE_LEVELS = {
    "offline_fixture",
    "local_live",
    "controlled_fault",
    "production",
}
ACTIVE_USERS = {"peak": 0}


@events.init.add_listener
def validate_profile(environment, **_kwargs) -> None:
    """Fail closed before spawning users when evidence/profile claims are unsafe."""
    runner = environment.runner
    if LOAD_PROFILE not in VALID_PROFILES:
        runner.quit()
        raise RuntimeError(f"Unsupported AUTOONCALL_LOAD_PROFILE={LOAD_PROFILE}")
    if EVIDENCE_LEVEL not in VALID_EVIDENCE_LEVELS:
        runner.quit()
        raise RuntimeError(f"Unsupported AUTOONCALL_EVIDENCE_LEVEL={EVIDENCE_LEVEL}")
    if LOAD_PROFILE == "fixture" and EVIDENCE_LEVEL != "offline_fixture":
        runner.quit()
        raise RuntimeError("fixture profile must use offline_fixture evidence")
    if LOAD_PROFILE == "real_model" and (
        not ENABLE_MODEL_REQUESTS
        or EVIDENCE_LEVEL not in {"local_live", "controlled_fault", "production"}
    ):
        runner.quit()
        raise RuntimeError(
            "real_model profile requires AUTOONCALL_ENABLE_MODEL_REQUESTS=true and a non-fixture "
            "evidence level"
        )


@events.test_start.add_listener
def record_test_start(environment, **_kwargs) -> None:
    environment.parsed_options.auto_oncall_started_at = time.time()
    ACTIVE_USERS["peak"] = 0


@events.spawning_complete.add_listener
def record_spawned_users(user_count, **_kwargs) -> None:
    """Capture peak users before the runner drains during shutdown."""
    ACTIVE_USERS["peak"] = max(ACTIVE_USERS["peak"], int(user_count))


@events.test_stop.add_listener
def write_load_artifact(environment, **_kwargs) -> None:
    """Persist a compact, provenance-bearing Locust summary for benchmark ingestion."""
    started_at = getattr(environment.parsed_options, "auto_oncall_started_at", time.time())
    rows = []
    for entry in environment.stats.entries.values():
        rows.append(
            {
                "method": entry.method,
                "name": entry.name,
                "request_count": entry.num_requests,
                "failure_count": entry.num_failures,
                "median_response_time_ms": entry.median_response_time,
                "p95_response_time_ms": entry.get_response_time_percentile(0.95),
                "p99_response_time_ms": entry.get_response_time_percentile(0.99),
                "average_response_time_ms": round(entry.avg_response_time, 2),
                "requests_per_second": round(entry.current_rps, 4),
            }
        )
    total = environment.stats.total
    payload = {
        "run": {
            "run_id": RUN_ID,
            "profile": LOAD_PROFILE,
            "evidence_level": EVIDENCE_LEVEL,
            "host": environment.host,
            "started_at_epoch": started_at,
            "finished_at_epoch": time.time(),
            "duration_seconds": round(max(0.0, time.time() - started_at), 2),
            "peak_user_count": ACTIVE_USERS["peak"],
        },
        "summary": {
            "request_count": total.num_requests,
            "failure_count": total.num_failures,
            "error_rate": (
                round(total.num_failures / total.num_requests, 4) if total.num_requests else None
            ),
            "median_response_time_ms": total.median_response_time,
            "p95_response_time_ms": total.get_response_time_percentile(0.95),
            "p99_response_time_ms": total.get_response_time_percentile(0.99),
            "average_response_time_ms": round(total.avg_response_time, 2),
            "requests_per_second": round(total.total_rps, 4),
        },
        "endpoints": sorted(rows, key=lambda item: (item["name"], item["method"])),
    }
    output_dir = Path(ARTIFACT_DIR) / "load-tests" / RUN_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "load_test.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# AutoOnCall Load Test",
        "",
        f"- Run ID: `{RUN_ID}`",
        f"- Profile: `{LOAD_PROFILE}`",
        f"- Evidence level: `{EVIDENCE_LEVEL}`",
        f"- Duration: `{payload['run']['duration_seconds']} s`",
        f"- Requests/failures: `{total.num_requests}/{total.num_failures}`",
        f"- RPS: `{payload['summary']['requests_per_second']}`",
        f"- P95/P99: `{payload['summary']['p95_response_time_ms']}/"
        f"{payload['summary']['p99_response_time_ms']} ms`",
        "",
    ]
    (output_dir / "load_test.md").write_text("\n".join(lines), encoding="utf-8")


class AutoOnCallUser(HttpUser):
    """Exercise health, read, alert, chat, and AIOps SSE endpoints."""

    wait_time = between(0.2, 1.0)

    @task(5)
    def health(self) -> None:
        self.client.get("/health/live", name="GET /health/live")

    @task(4)
    def incidents(self) -> None:
        self.client.get("/api/incidents?limit=20", name="GET /api/incidents")

    @task(3)
    def rag_chat(self) -> None:
        if LOAD_PROFILE == "fixture":
            return
        self.client.post(
            "/api/chat",
            json={
                "Id": f"{RUN_ID}-rag-{uuid4().hex[:8]}",
                "Question": "How should Redis maxclients saturation be diagnosed?",
            },
            name="POST /api/chat",
        )

    @task(2)
    def alert_ingestion(self) -> None:
        fingerprint = f"{RUN_ID}-{uuid4().hex}"
        self.client.post(
            "/api/alerts/alertmanager?auto_diagnose=false",
            json={
                "status": "firing",
                "alerts": [
                    {
                        "status": "firing",
                        "fingerprint": fingerprint,
                        "labels": {
                            "alertname": "RedisCapacity",
                            "service": "order-service",
                            "severity": "warning",
                            "evidence_level": EVIDENCE_LEVEL,
                            "load_run_id": RUN_ID,
                        },
                        "annotations": {"summary": "Redis capacity load-test warning"},
                    }
                ],
            },
            name="POST /api/alerts/alertmanager",
        )

    @task(1)
    def aiops_sse(self) -> None:
        if LOAD_PROFILE == "fixture":
            return
        body = {
            "session_id": f"{RUN_ID}-aiops-{uuid4().hex[:8]}",
            "incident": {
                "incident_id": f"INC-LOAD-{uuid4().hex[:12]}",
                "title": "order-service Redis saturation",
                "service_name": "order-service",
                "severity": "P2",
                "symptom": "Redis connection timeout and elevated 5xx",
                "environment": "load-test",
                "raw_alert": {
                    "evidence_level": EVIDENCE_LEVEL,
                    "load_run_id": RUN_ID,
                },
            },
        }
        with self.client.post(
            "/api/aiops",
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
            stream=True,
            catch_response=True,
            name="POST /api/aiops SSE",
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code}")
                return
            completed = False
            for line in response.iter_lines():
                if line and (
                    b'"type":"complete"' in line
                    or b'"type": "complete"' in line
                    or b'"type":"done"' in line
                    or b'"type": "done"' in line
                ):
                    completed = True
                    break
            if not completed:
                response.failure("SSE stream ended without a completion event")
