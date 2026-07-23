"""Verify repository-local Markdown links and runtime path references."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
ASCII_RUNTIME_PATH_RE = re.compile(
    r"(?:docs|eval|config|deploy|static|scripts)/[A-Za-z0-9_./-]+"
    r"\.(?:md|markdown|pdf|html|htm|csv|xlsx|yaml|yml|json|svg|png)"
)
DOC_PATH_TOKEN_RE = re.compile(r"docs[/\\][^\s`'\"<>|)]+")
EXTERNAL_URL_RE = re.compile(r"https?://[^\s`'\"<>|)]+")
IGNORED_REFERENCE_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "#",
    "/api/",
    "/health/",
    "/static/",
)
VERBATIM_EXTERNAL_ASSET_PREFIXES = (
    "/docs/",
    "/media/",
    "/llms",
)
VERBATIM_EXTERNAL_ASSET_NAMES = {
    "official_kubernetes_debug_pods.md",
    "official_kubernetes_debug_services.md",
    "official_kubernetes_pod_failure_reason.md",
    "official_loki_troubleshoot_ingest.md",
    "official_loki_troubleshoot_query.md",
    "official_prometheus_alerting_practices.md",
    "official_prometheus_alerting_rules.md",
    "official_redis_clients.md",
    "official_redis_latency.md",
}
TEXT_REFERENCE_FILES = (
    "README.md",
    "AGENTS.md",
    "Makefile",
    "Dockerfile",
    ".env.example",
)
TEXT_REFERENCE_ROOTS = (
    "app",
    "config",
    "deploy",
    "docs",
    "eval",
    "scripts",
    ".github",
)
TEXT_REFERENCE_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".sql",
    ".toml",
    ".bat",
    ".js",
    ".html",
    ".css",
}


@dataclass(frozen=True)
class ReferenceIssue:
    source: str
    line: int
    reference: str
    reason: str


def find_reference_issues(root: Path = REPO_ROOT) -> list[ReferenceIssue]:
    """Return broken local links, missing runtime paths, and non-ASCII docs paths."""
    issues: list[ReferenceIssue] = []
    for path in _iter_reference_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative_source = path.relative_to(root).as_posix()
        for line_number, line in enumerate(content.splitlines(), 1):
            local_reference_line = EXTERNAL_URL_RE.sub("", line)
            if path.suffix.lower() == ".md":
                for match in MARKDOWN_LINK_RE.finditer(line):
                    raw_target = match.group(1).strip()
                    if path.name in VERBATIM_EXTERNAL_ASSET_NAMES:
                        continue
                    issue = _validate_markdown_target(
                        root=root,
                        source_path=path,
                        source_label=relative_source,
                        line_number=line_number,
                        target=raw_target,
                    )
                    if issue:
                        issues.append(issue)
            for match in ASCII_RUNTIME_PATH_RE.finditer(local_reference_line):
                reference = match.group(0)
                if path.name == "knowledge-base-official-sources.md" or (
                    path.name in VERBATIM_EXTERNAL_ASSET_NAMES
                ):
                    continue
                if not (root / reference).exists():
                    issues.append(
                        ReferenceIssue(
                            source=relative_source,
                            line=line_number,
                            reference=reference,
                            reason="referenced path does not exist",
                        )
                    )
            for match in DOC_PATH_TOKEN_RE.finditer(local_reference_line):
                reference = match.group(0).rstrip(".,;:，。；：")
                if any(ord(char) > 127 for char in reference):
                    issues.append(
                        ReferenceIssue(
                            source=relative_source,
                            line=line_number,
                            reference=reference,
                            reason="docs runtime/reference path must be ASCII",
                        )
                    )
    return _dedupe_issues(issues)


def _iter_reference_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for name in TEXT_REFERENCE_FILES:
        path = root / name
        if path.is_file():
            seen.add(path)
            yield path
    for relative_root in TEXT_REFERENCE_ROOTS:
        directory = root / relative_root
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in TEXT_REFERENCE_SUFFIXES
                and path not in seen
            ):
                seen.add(path)
                yield path


def _validate_markdown_target(
    *,
    root: Path,
    source_path: Path,
    source_label: str,
    line_number: int,
    target: str,
) -> ReferenceIssue | None:
    cleaned = _clean_markdown_target(target)
    if not cleaned or cleaned.startswith(IGNORED_REFERENCE_PREFIXES):
        return None
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    path_text = unquote(cleaned.split("#", 1)[0]).replace("\\", "/")
    if not path_text:
        return None
    target_path = (
        (root / path_text).resolve()
        if path_text.startswith("docs/")
        else (source_path.parent / path_text).resolve()
    )
    if not _is_within(target_path, root):
        return ReferenceIssue(
            source=source_label,
            line=line_number,
            reference=target,
            reason="reference resolves outside repository",
        )
    if target_path.exists():
        return None
    return ReferenceIssue(
        source=source_label,
        line=line_number,
        reference=target,
        reason="Markdown target does not exist",
    )


def _clean_markdown_target(target: str) -> str:
    value = target.strip()
    if value.startswith("<") and ">" in value:
        return value[: value.index(">") + 1]
    if " " in value and not value.startswith(("http://", "https://")):
        value = value.split(" ", 1)[0]
    return value.strip("\"'")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _dedupe_issues(issues: list[ReferenceIssue]) -> list[ReferenceIssue]:
    unique = {(item.source, item.line, item.reference, item.reason): item for item in issues}
    return sorted(unique.values(), key=lambda item: (item.source, item.line, item.reference))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    issues = find_reference_issues(Path(args.root).resolve())
    if args.json:
        print(json.dumps([asdict(item) for item in issues], ensure_ascii=False, indent=2))
    elif issues:
        for item in issues:
            print(f"{item.source}:{item.line}: {item.reason}: {item.reference}")
    else:
        print("Repository references are valid.")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
