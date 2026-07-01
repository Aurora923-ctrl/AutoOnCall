"""Cleanup old AIOps trace, approval, and report records."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config
from app.services.aiops_store import create_aiops_store


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Cleanup old AIOps runtime records.")
    parser.add_argument("--database", default=None, help="SQLite database path; omit for configured backend.")
    parser.add_argument("--keep-days", type=int, default=config.log_retention_days)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    store = create_aiops_store(args.database)
    result = store.cleanup_older_than(keep_days=args.keep_days, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
