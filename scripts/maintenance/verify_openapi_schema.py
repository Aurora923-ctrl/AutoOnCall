"""Verify the committed frontend OpenAPI schema is current."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "static" / "generated" / "openapi.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.main import app

    expected = json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
    actual = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
    if actual != expected:
        print("Frontend OpenAPI schema is stale; run make frontend-schema.")
        return 1
    print("Frontend OpenAPI schema is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
