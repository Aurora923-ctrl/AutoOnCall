"""Check for local generated artifacts before demos or submission."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

IGNORED_DIR_NAMES = {".git"}

GENERATED_DIR_REASONS = {
    "__pycache__": "python cache directory",
    ".pytest_cache": "pytest cache directory",
    ".ruff_cache": "ruff cache directory",
    ".mypy_cache": "mypy cache directory",
    "htmlcov": "coverage html report directory",
    "logs": "runtime logs directory",
    "venv": "virtual environment directory",
    ".venv": "virtual environment directory",
    "env": "virtual environment directory",
}

GENERATED_FILE_REASONS = {
    ".coverage": "coverage data file",
    ".DS_Store": "macOS metadata file",
    "server.log": "runtime log file",
    "server.pid": "runtime pid file",
    "mcp_cls.log": "runtime log file",
    "mcp_cls.pid": "runtime pid file",
    "mcp_monitor.log": "runtime log file",
    "mcp_monitor.pid": "runtime pid file",
}

GENERATED_SUFFIX_REASONS = {
    ".pyc": "python bytecode file",
    ".pyo": "python bytecode file",
}


@dataclass(frozen=True)
class HygieneIssue:
    """A generated artifact that should not be included in a demo snapshot."""

    path: str
    kind: str
    reason: str


def find_hygiene_issues(root: Path, *, include_ignored: bool = False) -> list[HygieneIssue]:
    """Return generated artifact issues under root without deleting anything."""

    resolved_root = root.resolve()
    issues: list[HygieneIssue] = []
    seen: set[tuple[str, str, str]] = set()

    for current_dir, dir_names, file_names in os.walk(resolved_root):
        current_path = Path(current_dir)
        dir_names[:] = [name for name in dir_names if name not in IGNORED_DIR_NAMES]

        for dir_name in list(dir_names):
            path = current_path / dir_name
            reason = _directory_reason(path, resolved_root)
            if reason:
                _append_issue(
                    issues,
                    seen,
                    resolved_root,
                    path,
                    "directory",
                    reason,
                    include_ignored=include_ignored,
                )
                dir_names.remove(dir_name)

        for file_name in file_names:
            path = current_path / file_name
            reason = _file_reason(path, resolved_root)
            if reason:
                _append_issue(
                    issues,
                    seen,
                    resolved_root,
                    path,
                    "file",
                    reason,
                    include_ignored=include_ignored,
                )

    return sorted(issues, key=lambda issue: issue.path)


def _append_issue(
    issues: list[HygieneIssue],
    seen: set[tuple[str, str, str]],
    root: Path,
    path: Path,
    kind: str,
    reason: str,
    *,
    include_ignored: bool,
) -> None:
    relative_path = path.relative_to(root).as_posix()
    if not include_ignored and _is_git_ignored(root, relative_path):
        return
    key = (relative_path, kind, reason)
    if key in seen:
        return
    seen.add(key)
    issues.append(HygieneIssue(path=relative_path, kind=kind, reason=reason))


def _directory_reason(path: Path, root: Path) -> str:
    name = path.name
    if name in GENERATED_DIR_REASONS:
        return GENERATED_DIR_REASONS[name]
    if name.endswith(".egg-info"):
        return "python package metadata directory"
    relative_parts = path.relative_to(root).parts
    if relative_parts[:1] == ("uploads",) and len(relative_parts) > 1:
        return "uploaded demo artifact directory"
    return ""


def _file_reason(path: Path, root: Path) -> str:
    name = path.name
    if name in GENERATED_FILE_REASONS:
        return GENERATED_FILE_REASONS[name]
    if path.suffix in GENERATED_SUFFIX_REASONS:
        return GENERATED_SUFFIX_REASONS[path.suffix]

    relative_parts = path.relative_to(root).parts
    if len(relative_parts) >= 2 and relative_parts[0] == "data" and path.suffix == ".db":
        return "runtime sqlite database"
    if len(relative_parts) >= 2 and relative_parts[0] == "uploads":
        return "uploaded demo artifact file"
    return ""


def _is_git_ignored(root: Path, relative_path: str) -> bool:
    """Return True when Git ignore rules already cover a generated artifact."""
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative_path],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root to inspect")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--include-ignored",
        action="store_true",
        help="also report generated artifacts that are already covered by .gitignore",
    )
    args = parser.parse_args(argv)

    issues = find_hygiene_issues(Path(args.root), include_ignored=args.include_ignored)
    if args.json:
        print(json.dumps([asdict(issue) for issue in issues], ensure_ascii=False, indent=2))
    elif issues:
        print("Generated artifacts detected:")
        for issue in issues:
            print(f"- {issue.path} ({issue.kind}): {issue.reason}")
    else:
        print("No generated artifacts detected.")

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
