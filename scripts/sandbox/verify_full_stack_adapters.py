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

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ENV = ROOT / "deploy" / "sandbox.env"
DEFAULT_OUTPUT_PATH = ROOT / "logs" / "full_stack_adapter_verification.json"

REAL_SOURCE_BY_TOOL = {
    "query_metrics": "prometheus",
    "query_logs": "loki",
    "query_service_context": "cmdb",
    "query_deploy_history": "deploy_history",
    "query_redis_status": "redis_info",
    "query_mysql_status": "mysql",
    "search_history_ticket": "ticket_api",
}
DEFAULT_CHECKS = [
    {
        "tool_name": "query_metrics",
        "chain": "redis_maxclients",
        "input_args": {"service_name": "order-service", "time_range": "10m", "interval": "1m"},
        "expected_source": "prometheus",
    },
    {
        "tool_name": "query_logs",
        "chain": "redis_maxclients",
        "input_args": {
            "service_name": "order-service",
            "time_range": "24h",
            "query": "ERROR OR timeout",
            "limit": 20,
        },
        "expected_source": "loki",
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
        "tool_name": "query_redis_status",
        "chain": "redis_maxclients",
        "input_args": {
            "service_name": "order-service",
            "redis_instance": "redis-cluster-prod",
            "time_range": "10m",
        },
        "expected_source": "redis_info",
    },
    {
        "tool_name": "query_mysql_status",
        "chain": "mysql_slow_query",
        "input_args": {"service_name": "payment-service", "mysql_instance": "payment-mysql"},
        "expected_source": "mysql",
        "required_signals": {
            "slow_query_count": {"gte": 18},
            "pool_waiting": {"gte": 1},
            "active_connections": {"gte": 180},
        },
    },
    {
        "tool_name": "search_history_ticket",
        "chain": "redis_maxclients",
        "input_args": {"service_name": "order-service", "query": "redis timeout", "limit": 5},
        "expected_source": "ticket_api",
    },
    {
        "tool_name": "query_metrics",
        "chain": "mysql_slow_query",
        "input_args": {"service_name": "payment-service", "time_range": "10m", "interval": "1m"},
        "expected_source": "prometheus",
    },
    {
        "tool_name": "query_logs",
        "chain": "mysql_slow_query",
        "input_args": {
            "service_name": "payment-service",
            "time_range": "24h",
            "query": "slow query OR digest OR pool_waiting",
            "limit": 20,
        },
        "expected_source": "loki",
    },
    {
        "tool_name": "query_deploy_history",
        "chain": "mysql_slow_query",
        "input_args": {"service_name": "payment-service", "limit": 5},
        "expected_source": "deploy_history",
        "required_signals": {
            "deployment_count": {"gte": 1},
            "feature_flag_change": {"equals": True},
        },
    },
    {
        "tool_name": "search_history_ticket",
        "chain": "mysql_slow_query",
        "input_args": {
            "service_name": "payment-service",
            "query": "mysql slow query covering index feature flag",
            "limit": 5,
        },
        "expected_source": "ticket_api",
    },
]
GOLDEN_CHAINS = {
    "redis_maxclients": {
        "required_tools": [
            "query_redis_status",
            "query_metrics",
            "query_logs",
            "search_history_ticket",
        ],
        "required_sources": ["redis_info", "prometheus", "loki", "ticket_api"],
        "required_signals": {
            "query_redis_status": {
                "connected_clients": {"equals": 9940},
                "maxclients": {"equals": 10000},
                "client_usage_ratio": {"gte": 0.99},
                "blocked_clients": {"gte": 1},
            },
            "query_metrics": {
                "p95_latency_ms": {"gte": 1000},
                "error_rate": {"gte": 0.05},
            },
            "query_logs": {
                "log_count": {"gte": 1},
            },
            "search_history_ticket": {
                "ticket_count": {"gte": 1},
            },
        },
    },
    "mysql_slow_query": {
        "required_tools": [
            "query_mysql_status",
            "query_metrics",
            "query_logs",
            "query_deploy_history",
            "search_history_ticket",
        ],
        "required_sources": ["mysql", "prometheus", "loki", "deploy_history", "ticket_api"],
        "required_signals": {
            "query_mysql_status": {
                "slow_query_count": {"gte": 18},
                "pool_waiting": {"gte": 1},
                "active_connections": {"gte": 180},
            },
            "query_metrics": {
                "p95_latency_ms": {"gte": 2000},
                "error_rate": {"lte": 0.02},
                "cpu_usage_percent": {"gte": 70},
            },
            "query_logs": {
                "log_count": {"gte": 1},
            },
            "query_deploy_history": {
                "deployment_count": {"gte": 1},
                "feature_flag_change": {"equals": True},
            },
            "search_history_ticket": {
                "ticket_count": {"gte": 1},
            },
        },
    },
}


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
    missing_sources = sorted(set(REAL_SOURCE_BY_TOOL.values()).difference(data_sources))
    golden_chains = _golden_chain_summary(results)
    return {
        "status": "passed" if not failed else "failed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "data_sources": data_sources,
        "missing_sources": missing_sources,
        "failed_tools": list(dict.fromkeys(item["tool_name"] for item in failed)),
        "not_integrated": [],
        "mock_fallback_detected": any(item["observed_source"] == "mock" for item in results),
        "golden_chains": golden_chains,
        "golden_chain_count": len(golden_chains),
        "passed_golden_chain_count": sum(1 for item in golden_chains.values() if item["passed"]),
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
        signal_failures = _signal_failures(
            output.get("signals", {}) if isinstance(output.get("signals"), dict) else {},
            check.get("required_signals", {}),
        )
        if signal_failures:
            passed = False
        if fail_on_mock and observed_source in {"mock", "not_configured"}:
            passed = False
        return {
            "tool_name": tool_name,
            "chain": str(check.get("chain") or ""),
            "status": status,
            "passed": passed,
            "expected_source": expected_source,
            "observed_source": observed_source,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "summary": str(output.get("summary") or ""),
            "error_message": str(error_message or ""),
            "signals": output.get("signals", {}) if isinstance(output.get("signals"), dict) else {},
            "signal_failures": signal_failures,
        }
    except Exception as exc:
        return {
            "tool_name": tool_name,
            "chain": str(check.get("chain") or ""),
            "status": "failed",
            "passed": False,
            "expected_source": expected_source,
            "observed_source": "unknown",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "summary": "",
            "error_message": str(exc),
            "signals": {},
            "signal_failures": [],
        }


def _signal_failures(
    signals: dict[str, Any],
    requirements: Any,
) -> list[str]:
    if not isinstance(requirements, dict):
        return []
    failures: list[str] = []
    for key, expected in requirements.items():
        value = signals.get(key)
        if isinstance(expected, dict):
            if "gte" in expected and not (
                isinstance(value, int | float) and value >= float(expected["gte"])
            ):
                failures.append(f"{key} expected >= {expected['gte']}, got {value}")
            if "lte" in expected and not (
                isinstance(value, int | float) and value <= float(expected["lte"])
            ):
                failures.append(f"{key} expected <= {expected['lte']}, got {value}")
            if "equals" in expected and value != expected["equals"]:
                failures.append(f"{key} expected {expected['equals']}, got {value}")
            continue
        if value != expected:
            failures.append(f"{key} expected {expected}, got {value}")
    return failures


def _golden_chain_summary(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    chains: dict[str, dict[str, Any]] = {}
    for chain_name, spec in GOLDEN_CHAINS.items():
        by_tool = {
            str(item.get("tool_name")): item
            for item in results
            if str(item.get("chain") or "") in {"", chain_name}
        }
        required_tools = [str(item) for item in spec["required_tools"]]
        required_sources = [str(item) for item in spec["required_sources"]]
        tool_results = [by_tool.get(tool_name, {}) for tool_name in required_tools]
        missing_tools = [
            tool for tool, item in zip(required_tools, tool_results, strict=True) if not item
        ]
        failed_tools = [
            str(item.get("tool_name"))
            for item in tool_results
            if item and not bool(item.get("passed"))
        ]
        observed_sources = sorted(
            {
                str(item.get("observed_source"))
                for item in tool_results
                if item.get("observed_source") and item.get("observed_source") != "unknown"
            }
        )
        missing_sources = sorted(set(required_sources).difference(observed_sources))
        mock_sources = [
            str(item.get("tool_name"))
            for item in tool_results
            if item.get("observed_source") in {"mock", "not_configured"}
        ]
        signal_failures: list[str] = []
        required_signals = spec.get("required_signals", {})
        if isinstance(required_signals, dict):
            for tool_name, requirements in required_signals.items():
                item = by_tool.get(str(tool_name), {})
                failures = _signal_failures(
                    item.get("signals", {}) if isinstance(item.get("signals"), dict) else {},
                    requirements,
                )
                signal_failures.extend(f"{tool_name}: {failure}" for failure in failures)
        chains[chain_name] = {
            "passed": not missing_tools
            and not failed_tools
            and not missing_sources
            and not mock_sources
            and not signal_failures,
            "required_tools": required_tools,
            "required_sources": required_sources,
            "observed_sources": observed_sources,
            "missing_tools": missing_tools,
            "failed_tools": failed_tools,
            "missing_sources": missing_sources,
            "mock_or_unconfigured_tools": mock_sources,
            "signal_failures": signal_failures,
        }
    return chains


def _summary_text(
    failed: list[dict[str, Any]],
    data_sources: list[str],
    missing_sources: list[str],
) -> str:
    if failed:
        return "Interview adapter verification failed; failed_tools=" + ",".join(
            item["tool_name"] for item in failed
        )
    if missing_sources:
        return "All tools passed but expected sources are missing: " + ",".join(missing_sources)
    return "All configured interview adapters returned real data sources."


def write_report(payload: dict[str, Any], output_path: Path | None) -> None:
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify live AutoOnCall interview adapters.")
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
    from scripts.eval.eval_environment import collect_eval_environment

    registry = create_default_tool_registry([])
    payload = await verify_adapters(registry, fail_on_mock=not args.allow_mock)
    payload["run"] = {
        "environment": collect_eval_environment(
            suite="adapter_verification",
            evidence_level="local_live",
        ),
        "env_file": str(Path(args.env_file)),
        "scope": "live local Docker adapter verification",
    }
    write_report(payload, Path(args.output) if args.output else None)
    return payload


def render_console(payload: dict[str, Any]) -> str:
    lines = [
        f"Interview adapter verification: {payload['status'].upper()}",
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
    for name, chain in payload.get("golden_chains", {}).items():
        mark = "PASS" if chain["passed"] else "FAIL"
        lines.append(
            f"- {mark} chain:{name} sources={','.join(chain['observed_sources']) or '-'} "
            f"missing_sources={','.join(chain['missing_sources']) or '-'} "
            f"failed_tools={','.join(chain['failed_tools']) or '-'}"
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
