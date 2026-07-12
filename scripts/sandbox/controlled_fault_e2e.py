"""Run one-window Redis/MySQL fault injection, diagnosis, and recovery experiments."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sandbox.controlled_fault import (  # noqa: E402
    ControlledFaultRunner,
    ExperimentSpec,
    write_run_artifacts,
)
from scripts.sandbox.verify_mysql_mainline_stability import (  # noqa: E402
    load_env_file,
    run_once as run_mysql_once,
)
from scripts.sandbox.verify_redis_mainline_stability import (  # noqa: E402
    run_once as run_redis_once,
)

DEFAULT_OUTPUT_ROOT = ROOT / "logs" / "controlled_fault"
DEFAULT_ENV_FILE = ROOT / "deploy" / "sandbox.env"


def build_specs() -> list[ExperimentSpec]:
    return [
        ExperimentSpec(
            experiment_id="cf-e2e-redis-01",
            fault_type="redis_capacity",
            target="autooncall-redis",
            parameters={
                "maxclients": 16,
                "connection_count": 12,
                "hold_seconds": 0.1,
            },
            ground_truth="redis_maxclients",
        ),
        ExperimentSpec(
            experiment_id="cf-e2e-mysql-01",
            fault_type="mysql_slow_query",
            target="autooncall-mysql",
            parameters={
                "sleep_seconds": 3.0,
                "concurrency": 8,
                "hold_seconds": 0.1,
            },
            ground_truth="mysql_slow_query",
        ),
    ]


def run_diagnosis(spec: ExperimentSpec) -> dict[str, Any]:
    if spec.fault_type == "redis_capacity":
        result = asyncio.run(run_redis_once(1))
    elif spec.fault_type == "mysql_slow_query":
        result = asyncio.run(run_mysql_once(1))
    else:
        raise ValueError(f"Unsupported end-to-end fault type: {spec.fault_type}")
    return {
        "top_1_rca": result.get("root_cause_category"),
        "top_3_rca": [result.get("root_cause_category")],
        "tools": result.get("executed_tools", []),
        "data_sources": result.get("sources", []),
        "evidence_completeness": bool(result.get("passed")),
        "replan_triggered": False,
        "needs_human": result.get("report_status") == "needs_human",
        "report_status": result.get("report_status"),
        "root_cause": result.get("root_cause"),
        "business_signature_passed": bool(result.get("passed")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    run_id = args.run_id or datetime.now(UTC).strftime("controlled-fault-e2e-%Y%m%dT%H%M%SZ")
    runner = ControlledFaultRunner(
        dry_run=False,
        acknowledged_local_only=True,
        diagnosis_runner=run_diagnosis,
    )
    records = [runner.run(spec) for spec in build_specs()]
    summary = write_run_artifacts(
        output_dir=args.output_root,
        run_id=run_id,
        records=records,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(record["status"] == "passed" for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
