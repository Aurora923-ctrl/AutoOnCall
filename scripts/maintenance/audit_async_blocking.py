"""Fail when known blocking I/O is called directly inside async functions."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = ROOT / "app"
BLOCKING_CALLS = {
    "open",
    "Path.open",
    "Path.read_bytes",
    "Path.read_text",
    "Path.write_bytes",
    "Path.write_text",
    "sqlite3.connect",
    "pymysql.connect",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "time.sleep",
}
ASYNC_CALLS = {"aiofiles.open"}


def audit_file(path: Path) -> list[str]:
    """Return async blocking-call findings for one Python file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = _call_name(child.func)
            if name in ASYNC_CALLS:
                continue
            if name in BLOCKING_CALLS or name.endswith(
                (".open", ".read_bytes", ".read_text", ".write_bytes", ".write_text")
            ):
                findings.append(f"{path.relative_to(ROOT)}:{child.lineno}: {name}")
    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def main() -> int:
    findings = [finding for path in APP_ROOT.rglob("*.py") for finding in audit_file(path)]
    if findings:
        print("Blocking calls found inside async code:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Async blocking-call audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
