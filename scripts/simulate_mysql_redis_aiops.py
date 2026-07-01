"""Run local Redis/MySQL/Prometheus AIOps full-stack scenarios through FastAPI/LangGraph."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_COMPOSE = ROOT / "deploy" / "full-stack-compose.yml"
SANDBOX_ENV = ROOT / "deploy" / "sandbox.env"
SANDBOX_REDIS_CONTAINER = "autooncall-full-redis"
SANDBOX_MYSQL_CONTAINER = "autooncall-full-mysql"
SANDBOX_PROMETHEUS_CONTAINER = "autooncall-full-prometheus"
SANDBOX_EXPORTER_CONTAINER = "autooncall-full-metrics-exporter"


class EmptyMCPClient:
    async def get_tools(self) -> list[Any]:
        return []


class FailingPlannerLLM:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("LLM disabled for deterministic local simulation")


async def fake_get_mcp_client_with_retry() -> EmptyMCPClient:
    return EmptyMCPClient()


def raise_disabled_llm() -> Any:
    raise RuntimeError("LLM disabled for deterministic local simulation")


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                events.append(json.loads("\n".join(data_lines)))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        events.append(json.loads("\n".join(data_lines)))
    return events


def docker(*args: str) -> str:
    completed = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def docker_best_effort(*args: str) -> str:
    completed = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return (completed.stdout or completed.stderr).strip()


def docker_compose(*args: str) -> str:
    return docker("compose", "-f", str(SANDBOX_COMPOSE), *args)


def docker_compose_best_effort(*args: str) -> str:
    return docker_best_effort("compose", "-f", str(SANDBOX_COMPOSE), *args)


def load_sandbox_env(path: Path = SANDBOX_ENV, *, override: bool = False) -> None:
    """Load sandbox adapter settings before importing app.config."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if override or key not in os.environ:
            os.environ[key] = value


def prometheus_ready_url() -> str:
    base_url = os.environ.get("PROMETHEUS_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("PROMETHEUS_BASE_URL must be configured via deploy/sandbox.env")
    return f"{base_url}/-/ready"


def ensure_sandbox_started() -> None:
    docker_compose("up", "-d")


def patch_runtime_for_deterministic_full_chain() -> None:
    from app.services.aiops_service import AIOpsService

    planner_module = importlib.import_module("app.agent.aiops.planner")
    executor_module = importlib.import_module("app.agent.aiops.executor")
    replanner_module = importlib.import_module("app.agent.aiops.replanner")
    aiops_api = importlib.import_module("app.api.aiops")

    planner_module.ChatQwen = FailingPlannerLLM
    planner_module.retrieve_structured_knowledge = lambda _: {"status": "empty"}
    planner_module.get_mcp_client_with_retry = fake_get_mcp_client_with_retry
    executor_module.get_mcp_client_with_retry = fake_get_mcp_client_with_retry
    replanner_module._create_llm = raise_disabled_llm
    aiops_api.aiops_service = AIOpsService()


def build_app() -> FastAPI:
    from app.api import aiops, approvals, incidents

    patch_runtime_for_deterministic_full_chain()
    app = FastAPI()
    app.include_router(aiops.router, prefix="/api")
    app.include_router(approvals.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    return app


async def noop() -> None:
    return None


async def stop_container(name: str) -> None:
    docker_best_effort("stop", name)


async def start_container(name: str) -> None:
    docker_best_effort("start", name)
    if name == SANDBOX_MYSQL_CONTAINER:
        await wait_for_mysql()
    elif name == SANDBOX_REDIS_CONTAINER:
        await wait_for_redis()
    elif name == SANDBOX_PROMETHEUS_CONTAINER:
        await wait_for_prometheus()
    else:
        await asyncio.sleep(2)


async def wait_for_mysql(timeout_seconds: int = 25) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        output = docker_best_effort(
            "exec",
            SANDBOX_MYSQL_CONTAINER,
            "mysqladmin",
            "-uautooncall",
            "-pautooncall123",
            "ping",
        )
        if "mysqld is alive" in output:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Timed out waiting for {SANDBOX_MYSQL_CONTAINER} to become ready")


async def wait_for_redis(timeout_seconds: int = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        output = docker_best_effort("exec", SANDBOX_REDIS_CONTAINER, "redis-cli", "ping")
        if "PONG" in output:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"Timed out waiting for {SANDBOX_REDIS_CONTAINER} to become ready")


async def wait_for_prometheus(timeout_seconds: int = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=3) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(prometheus_ready_url())
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"Timed out waiting for {SANDBOX_PROMETHEUS_CONTAINER} to become ready")


async def wait_for_sandbox() -> None:
    await asyncio.gather(wait_for_mysql(), wait_for_redis(), wait_for_prometheus())


async def configure_redis_risk() -> None:
    docker_best_effort("start", SANDBOX_REDIS_CONTAINER)
    await asyncio.sleep(1)
    docker_best_effort(
        "exec", SANDBOX_REDIS_CONTAINER, "redis-cli", "CONFIG", "SET", "maxclients", "1"
    )
    docker_best_effort("exec", SANDBOX_REDIS_CONTAINER, "redis-cli", "SLOWLOG", "RESET")
    docker_best_effort(
        "exec",
        SANDBOX_REDIS_CONTAINER,
        "redis-cli",
        "CONFIG",
        "SET",
        "slowlog-log-slower-than",
        "0",
    )
    docker_best_effort(
        "exec",
        SANDBOX_REDIS_CONTAINER,
        "redis-cli",
        "SET",
        "autooncall:slowlog:probe",
        "1",
    )


async def restore_redis_config() -> None:
    docker_best_effort("start", SANDBOX_REDIS_CONTAINER)
    await asyncio.sleep(1)
    docker_best_effort(
        "exec",
        SANDBOX_REDIS_CONTAINER,
        "redis-cli",
        "CONFIG",
        "SET",
        "maxclients",
        "10000",
    )
    docker_best_effort(
        "exec",
        SANDBOX_REDIS_CONTAINER,
        "redis-cli",
        "CONFIG",
        "SET",
        "slowlog-log-slower-than",
        "1000",
    )
    docker_best_effort("exec", SANDBOX_REDIS_CONTAINER, "redis-cli", "SLOWLOG", "RESET")


async def generate_mysql_slow_query_signal() -> None:
    docker_best_effort("start", SANDBOX_MYSQL_CONTAINER)
    await wait_for_mysql()
    docker_best_effort(
        "exec",
        SANDBOX_MYSQL_CONTAINER,
        "mysql",
        "-uroot",
        "-p123456",
        "-e",
        "SET GLOBAL long_query_time=0; SET GLOBAL slow_query_log=ON;",
    )
    docker_best_effort(
        "exec",
        SANDBOX_MYSQL_CONTAINER,
        "mysql",
        "-uautooncall",
        "-pautooncall123",
        "-D",
        "autooncall",
        "-e",
        "SELECT SLEEP(1);",
    )


async def restore_mysql_slow_query_threshold() -> None:
    docker_best_effort("start", SANDBOX_MYSQL_CONTAINER)
    await wait_for_mysql()
    docker_best_effort(
        "exec",
        SANDBOX_MYSQL_CONTAINER,
        "mysql",
        "-uroot",
        "-p123456",
        "-e",
        "SET GLOBAL long_query_time=10;",
    )


async def run_incident(
    client: httpx.AsyncClient,
    *,
    title: str,
    service_name: str,
    severity: str,
    symptom: str,
    environment: str = "prod",
) -> dict[str, Any]:
    session_id = f"sim-{uuid4().hex}"
    response = await client.post(
        "/api/aiops",
        json={
            "session_id": session_id,
            "incident": {
                "title": title,
                "service_name": service_name,
                "severity": severity,
                "symptom": symptom,
                "environment": environment,
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    events = parse_sse_events(response.text)
    complete = events[-1] if events else {}
    incident_id = complete.get("incident_id", "")

    trace_payload: dict[str, Any] = {"items": []}
    report_payload: dict[str, Any] = {"report": None}
    if incident_id:
        trace_response = await client.get(f"/api/incidents/{incident_id}/trace")
        report_response = await client.get(f"/api/incidents/{incident_id}/report")
        trace_payload = (
            trace_response.json() if trace_response.status_code == 200 else trace_payload
        )
        report_payload = (
            report_response.json() if report_response.status_code == 200 else report_payload
        )

    tool_calls = [
        event for event in trace_payload.get("items", []) if event.get("event_type") == "tool_call"
    ]
    return {
        "session_id": session_id,
        "incident_id": incident_id,
        "event_types": [event.get("type") for event in events],
        "complete_status": complete.get("status"),
        "report_status": (complete.get("structured_report") or {}).get("status"),
        "root_cause": (complete.get("structured_report") or {}).get("root_cause"),
        "trace_tool_calls": tool_calls,
        "report": report_payload.get("report"),
    }


def summarize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for event in tool_calls:
        metadata = event.get("metadata") or {}
        summary.append(
            {
                "tool_name": metadata.get("tool_name"),
                "status": metadata.get("status") or event.get("status"),
                "data_source": metadata.get("data_source"),
                "output_summary": event.get("output_summary"),
                "error": event.get("error_message"),
            }
        )
    return summary


def summarize_report_tool_calls(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    return [
        {
            "tool_name": call.get("tool_name"),
            "status": call.get("status"),
            "data_source": call.get("data_source"),
            "output_summary": call.get("output_summary"),
            "error": call.get("error_message"),
        }
        for call in report.get("tool_calls", [])
    ]


def collect_data_sources(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        calls = result.get("tool_call_summary") or result.get("trace_tool_call_summary") or []
        for call in calls:
            source = call.get("data_source") or "unknown"
            counts[source] = counts.get(source, 0) + 1
    return counts


async def run_scenario(
    client: httpx.AsyncClient,
    *,
    name: str,
    before: Callable[[], Awaitable[None]] = noop,
    after: Callable[[], Awaitable[None]] = noop,
    incident: dict[str, str],
) -> dict[str, Any]:
    await before()
    try:
        result = await run_incident(client, **incident)
        result["name"] = name
        result["trace_tool_call_summary"] = summarize_tool_calls(result["trace_tool_calls"])
        result["tool_call_summary"] = summarize_report_tool_calls(result.get("report"))
        result.pop("trace_tool_calls", None)
        return result
    finally:
        await after()


async def main() -> None:
    load_sandbox_env()
    ensure_sandbox_started()
    await wait_for_sandbox()

    app = build_app()
    transport = httpx.ASGITransport(app=app)
    scenarios = [
        {
            "name": "redis_normal",
            "incident": {
                "title": "order-service Redis timeout check",
                "service_name": "order-service",
                "severity": "P2",
                "symptom": "Redis connection timeout, 5xx increased, verify Redis health",
            },
        },
        {
            "name": "mysql_normal",
            "incident": {
                "title": "payment-service MySQL latency check",
                "service_name": "payment-service",
                "severity": "P2",
                "symptom": "MySQL slow query and connection pool waiting, verify MySQL health",
            },
        },
        {
            "name": "redis_down",
            "before": lambda: stop_container(SANDBOX_REDIS_CONTAINER),
            "after": lambda: start_container(SANDBOX_REDIS_CONTAINER),
            "incident": {
                "title": "order-service Redis outage",
                "service_name": "order-service",
                "severity": "P1",
                "symptom": "Redis connection refused and API 5xx increased",
            },
        },
        {
            "name": "redis_low_maxclients_slowlog",
            "before": configure_redis_risk,
            "after": restore_redis_config,
            "incident": {
                "title": "order-service Redis maxclients and slowlog risk",
                "service_name": "order-service",
                "severity": "P1",
                "symptom": "Redis maxclients risk and slow command warning, verify Redis health",
            },
        },
        {
            "name": "mysql_down",
            "before": lambda: stop_container(SANDBOX_MYSQL_CONTAINER),
            "after": lambda: start_container(SANDBOX_MYSQL_CONTAINER),
            "incident": {
                "title": "payment-service MySQL outage",
                "service_name": "payment-service",
                "severity": "P1",
                "symptom": "MySQL connection refused and request latency increased",
            },
        },
        {
            "name": "mysql_slow_query_counter",
            "before": generate_mysql_slow_query_signal,
            "after": restore_mysql_slow_query_threshold,
            "incident": {
                "title": "payment-service MySQL slow query counter",
                "service_name": "payment-service",
                "severity": "P2",
                "symptom": "MySQL slow query counter increased, verify slow query status",
            },
        },
        {
            "name": "redis_mysql_recovered",
            "incident": {
                "title": "checkout-service dependency recovery check",
                "service_name": "checkout-service",
                "severity": "P2",
                "symptom": "Latency timeout after dependency recovery, verify Redis and MySQL",
            },
        },
    ]

    results = []
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for scenario in scenarios:
            results.append(await run_scenario(client, **scenario))

    docker_compose_best_effort("up", "-d")
    output_path = ROOT / "logs" / "sandbox_aiops_simulation.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "sandbox_services": [
            SANDBOX_REDIS_CONTAINER,
            SANDBOX_MYSQL_CONTAINER,
            SANDBOX_PROMETHEUS_CONTAINER,
            SANDBOX_EXPORTER_CONTAINER,
        ],
        "data_sources": collect_data_sources(results),
        "results": results,
    }
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
