"""CLI for the local-only controlled-fault experiment plan."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = ROOT / "scripts" / "sandbox" / "controlled_fault_plan.json"
DEFAULT_OUTPUT_ROOT = ROOT / "logs" / "controlled_fault"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sandbox.controlled_fault import (  # noqa: E402
    ControlledFaultRunner,
    load_specs,
    write_run_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually inject bounded faults. The default is a non-mutating dry-run.",
    )
    parser.add_argument(
        "--acknowledge-local-only",
        action="store_true",
        help="Required with --execute; confirms the allowlisted local sandbox target.",
    )
    parser.add_argument("--diagnosis-url", default="")
    parser.add_argument(
        "--experiment-id",
        action="append",
        default=[],
        help="Run only the named experiment ID; repeat to select multiple experiments.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    specs = load_specs(args.plan)
    if args.experiment_id:
        requested = set(args.experiment_id)
        specs = [spec for spec in specs if spec.experiment_id in requested]
        missing = sorted(requested - {spec.experiment_id for spec in specs})
        if missing:
            raise SystemExit(f"Unknown experiment IDs: {', '.join(missing)}")
    run_id = args.run_id or datetime.now(UTC).strftime("controlled-fault-%Y%m%dT%H%M%SZ")
    runner = ControlledFaultRunner(
        dry_run=not args.execute,
        acknowledged_local_only=args.acknowledge_local_only,
        diagnosis_url=args.diagnosis_url,
    )
    records = [runner.run(spec) for spec in specs]
    summary = write_run_artifacts(
        output_dir=args.output_root,
        run_id=run_id,
        records=records,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if any(record["status"] == "failed" for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
