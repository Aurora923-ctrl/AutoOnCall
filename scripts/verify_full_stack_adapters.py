"""Verify that AIOps tools consume live full-stack adapter data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ENV = ROOT / "deploy" / "sandbox.env"
DEFAULT_OUTPUT_PATH = ROOT / "logs" / "full_stack_adapter_verification.json"

EXPECTED_NOT_INTEGRATED: list[str] = []
REAL_SOURCE_BY_TOOL = {
    "query_alerts": "alertmanager",
    "query_metrics": "prometheus",
    "query_logs": "loki",
    "query_k8s_status": "kubernetes",
    "query_traces": "jaeger",
    "query_service_context": "cmdb",
    "query_deploy_history": "deploy_history",
    "query_message_queue_status": "redpanda",
    "query_redis_status": "redis_info",
    "query_mysql_status": "mysql",
    "search_history_ticket": "ticket_api",
}
DEFAULT_CHECKS = [
    {
        "tool_name": "query_alerts",
        "input_args": {"service_name": "order-service", "state": "active", "limit": 20},
        "expected_source": "alertmanager",
    },
    {
        "tool_name": "query_metrics",
        "input_args": {"service_name": "order-service", "time_range": "10m", "interval": "1m"},
        "expected_source": "prometheus",
    },
    {
        "tool_name": "query_logs",
        "input_args": {
            "service_name": "order-service",
            "time_range": "24h",
            "query": "ERROR OR timeout",
            "limit": 20,
        },
        "expected_source": "loki",
    },
    {
        "tool_name": "query_k8s_status",
        "input_args": {"service_name": "inventory-service", "time_range": "10m"},
        "expected_source": "kubernetes",
    },
    {
        "tool_name": "query_traces",
        "input_args": {"service_name": "order-service", "lookback": "1h", "limit": 20},
        "expected_source": "jaeger",
    },
    {
        "tool_name": "query_service_context",
        "input_args": {"service_name": "order-service"},
        "expected_source": "cmdb",
    },
    {
        "tool_name": "query_deploy_history",
        "input_args": {"service_name": "order-service", "limit": 5},
        "expected_source": "deploy_history",
    },
    {
        "tool_name": "query_message_queue_status",
        "input_args": {"service_name": "checkout-service", "topic": ""},
        "expected_source": "redpanda",
    },
    {
        "tool_name": "query_redis_status",
        "input_args": {
            "service_name": "order-service",
            "redis_instance": "redis-cluster-prod",
            "time_range": "10m",
        },
        "expected_source": "redis_info",
    },
    {
        "tool_name": "query_mysql_status",
        "input_args": {"service_name": "order-service", "mysql_instance": "order-mysql"},
        "expected_source": "mysql",
    },
    {
        "tool_name": "search_history_ticket",
        "input_args": {"service_name": "order-service", "query": "redis timeout", "limit": 5},
        "expected_source": "ticket_api",
    },
]


def load_env_file(path: Path, *, override: bool = False) -> None:
    """Load key-value env files before importing app.config."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if override or key not in os.environ:
            os.environ[key] = value


async def verify_adapters(
    registry: Any,
    checks: Iterable[dict[str, Any]] = DEFAULT_CHECKS,
    *,
    fail_on_mock: bool = True,
) -> dict[str, Any]:
    """Run adapter tools and return a deterministic verification report."""
    started = time.perf_counter()
    results = []
    for check in checks:
        results.append(await _run_check(registry, check, fail_on_mock=fail_on_mock))

    failed = [item for item in results if not item["passed"]]
    data_sources = sorted(
        {
            item["observed_source"]
            for item in results
            if item["observed_source"] and item["observed_source"] != "unknown"
        }
    )
    missing_sources = sorted(
        set(REAL_SOURCE_BY_TOOL.values()).difference(data_sources)
    )
    return {
        "status": "passed" if not failed else "failed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "data_sources": data_sources,
        "missing_sources": missing_sources,
        "failed_tools": [item["tool_name"] for item in failed],
        "not_integrated": EXPECTED_NOT_INTEGRATED,
        "mock_fallback_detected": any(item["observed_source"] == "mock" for item in results),
        "checks": results,
        "summary": _summary_text(failed, data_sources, missing_sources),
    }


async def _run_check(
    registry: Any,
    check: dict[str, Any],
    *,
    fail_on_mock: bool,
) -> dict[str, Any]:
    tool_name = str(check["tool_name"])
    expected_source = str(check["expected_source"])
    input_args = dict(check.get("input_args") or {})
    started = time.perf_counter()
    try:
        result = await registry.arun(tool_name, input_args)
        output = result.output if isinstance(result.output, dict) else {}
        observed_source = str(output.get("source") or "unknown")
        status = str(getattr(result, "status", "failed"))
        error_message = getattr(result, "error_message", None) or output.get("error_message", "")
        passed = status == "success" and observed_source == expected_source
        if fail_on_mock and observed_source in {"mock", "not_configured"}:
            passed = False
        return {
            "tool_name": tool_name,
            "status": status,
            "passed": passed,
            "expected_source": expected_source,
            "observed_source": observed_source,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "summary": str(output.get("summary") or ""),
            "error_message": str(error_message or ""),
            "signals": output.get("signals", {}) if isinstance(output.get("signals"), dict) else {},
        }
    except Exception as exc:
        return {
            "tool_name": tool_name,
            "status": "failed",
            "passed": False,
            "expected_source": expected_source,
            "observed_source": "unknown",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "summary": "",
            "error_message": str(exc),
            "signals": {},
        }


def _summary_text(
    failed: list[dict[str, Any]],
    data_sources: list[str],
    missing_sources: list[str],
) -> str:
    if failed:
        return (
            "Full-stack adapter verification failed; failed_tools="
            + ",".join(item["tool_name"] for item in failed)
        )
    if missing_sources:
        return "All tools passed but expected sources are missing: " + ",".join(missing_sources)
    return "All configured full-stack adapters returned real data sources."


def write_report(payload: dict[str, Any], output_path: Path | None) -> None:
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify live AutoOnCall full-stack adapters.")
    parser.add_argument("--env-file", default=str(SANDBOX_ENV))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    parser.add_argument(
        "--allow-mock",
        action="store_true",
        help="Do not fail checks that return mock/not_configured sources",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file), override=False)
    os.environ.setdefault("AIOPS_MOCK_FALLBACK_ENABLED", "false")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from app.tools.registry import create_default_tool_registry

    registry = create_default_tool_registry([])
    payload = await verify_adapters(registry, fail_on_mock=not args.allow_mock)
    write_report(payload, Path(args.output) if args.output else None)
    return payload


def render_console(payload: dict[str, Any]) -> str:
    lines = [
        f"Full-stack adapter verification: {payload['status'].upper()}",
        f"Data sources: {', '.join(payload['data_sources']) or '-'}",
        f"Missing sources: {', '.join(payload['missing_sources']) or '-'}",
        f"Not integrated yet: {', '.join(payload['not_integrated'])}",
    ]
    for item in payload["checks"]:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(
            f"- {mark} {item['tool_name']} source={item['observed_source']} "
            f"expected={item['expected_source']} status={item['status']} "
            f"latency={item['latency_ms']}ms"
        )
    lines.append(payload["summary"])
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    payload = asyncio.run(main_async(args))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_console(payload))
        if args.output:
            print(f"Report: {args.output}")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
