"""Verify that supported installation paths consume the committed uv lockfile."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_DEPENDENCIES = (
    "langchain",
    "langchain-community",
    "langchain-core",
    "langgraph",
    "langchain-milvus",
    "langchain-text-splitters",
    "langchain-mcp-adapters",
    "langchain-qwq",
)


def main() -> int:
    failures: list[str] = []
    pyproject = _read("pyproject.toml")
    dockerfile = _read("Dockerfile")
    workflow = _read(".github/workflows/quality.yml")
    makefile = _read("Makefile")

    if not (REPO_ROOT / "uv.lock").is_file():
        failures.append("uv.lock is missing")

    for dependency in AI_DEPENDENCIES:
        pattern = rf'"{re.escape(dependency)}>=[^"]+,<[^"]+"'
        if re.search(pattern, pyproject) is None:
            failures.append(f"{dependency} must declare a compatibility upper bound")

    required_snippets = {
        "Dockerfile": (
            "COPY pyproject.toml uv.lock README.md ./",
            "uv sync --locked --no-dev --no-editable",
        ),
        ".github/workflows/quality.yml": ("uv sync --locked --extra dev",),
        "Makefile": (
            "uv sync --locked --no-dev",
            "uv sync --locked --extra dev",
        ),
    }
    contents = {
        "Dockerfile": dockerfile,
        ".github/workflows/quality.yml": workflow,
        "Makefile": makefile,
    }
    for path, snippets in required_snippets.items():
        for snippet in snippets:
            if snippet not in contents[path]:
                failures.append(f"{path} must contain `{snippet}`")

    forbidden_installers = {
        "Dockerfile": ("pip install .",),
        ".github/workflows/quality.yml": ('pip install -e ".[dev]"',),
    }
    for path, snippets in forbidden_installers.items():
        for snippet in snippets:
            if snippet in contents[path]:
                failures.append(f"{path} must not contain `{snippet}`")

    if failures:
        print("Dependency lock verification failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Dependency lock verification passed.")
    return 0


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
