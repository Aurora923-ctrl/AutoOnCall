"""Reset AIOps runtime storage to the four curated interview incidents."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from scripts.data.seed_demo_data import seed_demo_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Explicit SQLite path. Omit to reset the configured backend.",
    )
    parser.add_argument(
        "--backend",
        choices=("sqlite", "mysql"),
        default=config.aiops_storage_backend,
        help="Configured backend used when --database is omitted.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="Required confirmation because reset deletes all current AIOps runtime data.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_reset:
        raise SystemExit("Refusing to reset runtime data without --confirm-reset")
    result = seed_demo_data(
        database_path=args.database,
        backend=args.backend,
        reset=True,
    )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
