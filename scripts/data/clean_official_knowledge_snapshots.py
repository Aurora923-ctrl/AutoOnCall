"""Download and create retrieval-focused snapshots from official documentation."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs" / "knowledge-base"
SNAPSHOT_DATE = "2026-07-21"
DOCS_SEGMENT = "do" + "cs"

OFFICIAL_SOURCES = {
    "official_kubernetes_debug_pods.md": {
        "source": (
            "https://github.com/kubernetes/website/blob/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/debug-pods.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/kubernetes/website/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/debug-pods.md"
        ),
        "revision": "c3317651dc19ef683c5c4463bb6bf0602c0bf364",
        "license": "CC BY 4.0",
    },
    "official_kubernetes_debug_services.md": {
        "source": (
            "https://github.com/kubernetes/website/blob/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/debug-service.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/kubernetes/website/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/debug-service.md"
        ),
        "revision": "c3317651dc19ef683c5c4463bb6bf0602c0bf364",
        "license": "CC BY 4.0",
    },
    "official_kubernetes_pod_failure_reason.md": {
        "source": (
            "https://github.com/kubernetes/website/blob/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/determine-reason-pod-failure.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/kubernetes/website/"
            "c3317651dc19ef683c5c4463bb6bf0602c0bf364/content/en/docs/tasks/debug/"
            "debug-application/determine-reason-pod-failure.md"
        ),
        "revision": "c3317651dc19ef683c5c4463bb6bf0602c0bf364",
        "license": "CC BY 4.0",
    },
    "official_prometheus_alerting_practices.md": {
        "source": (
            "https://github.com/prometheus/docs/blob/"
            "47c3b182327d2832daadb00d0beacfcd802e4458/"
            + DOCS_SEGMENT
            + "/practices/alerting.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/prometheus/docs/"
            "47c3b182327d2832daadb00d0beacfcd802e4458/"
            + DOCS_SEGMENT
            + "/practices/alerting.md"
        ),
        "revision": "47c3b182327d2832daadb00d0beacfcd802e4458",
        "license": "Apache-2.0",
    },
    "official_prometheus_alerting_rules.md": {
        "source": (
            "https://github.com/prometheus/prometheus/blob/"
            "2cf323988931bd586a2ab25160e46bcace9398ae/"
            + DOCS_SEGMENT
            + "/configuration/alerting_rules.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/prometheus/prometheus/"
            "2cf323988931bd586a2ab25160e46bcace9398ae/"
            + DOCS_SEGMENT
            + "/configuration/alerting_rules.md"
        ),
        "revision": "2cf323988931bd586a2ab25160e46bcace9398ae",
        "license": "Apache-2.0",
    },
    "official_redis_clients.md": {
        "source": (
            "https://github.com/redis/docs/blob/"
            "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/redis/docs/"
            "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/develop/reference/clients.md"
        ),
        "revision": "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68",
        "license": "CC BY-NC-SA 4.0 and upstream notices",
    },
    "official_redis_latency.md": {
        "source": (
            "https://github.com/redis/docs/blob/"
            "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/"
            "management/optimization/latency.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/redis/docs/"
            "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68/content/operate/oss_and_stack/"
            "management/optimization/latency.md"
        ),
        "revision": "36a9e2dbb407116f2a9d46d0f600cebdf8e4be68",
        "license": "CC BY-NC-SA 4.0 and upstream notices",
    },
    "official_loki_troubleshoot_ingest.md": {
        "source": (
            "https://github.com/grafana/loki/blob/"
            "925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/"
            "troubleshooting/troubleshoot-ingest.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/grafana/loki/"
            "925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/operations/"
            "troubleshooting/troubleshoot-ingest.md"
        ),
        "revision": "925c8c7c7c6feface41c5bef12c74f05c05e8c84",
        "license": "Grafana documentation terms; upstream repository AGPL-3.0",
    },
    "official_loki_troubleshoot_query.md": {
        "source": (
            "https://github.com/grafana/loki/blob/"
            "925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/"
            "troubleshoot-query.md"
        ),
        "raw": (
            "https://raw.githubusercontent.com/grafana/loki/"
            "925c8c7c7c6feface41c5bef12c74f05c05e8c84/docs/sources/shared/"
            "troubleshoot-query.md"
        ),
        "revision": "925c8c7c7c6feface41c5bef12c74f05c05e8c84",
        "license": "Grafana documentation terms; upstream repository AGPL-3.0",
    },
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Clean current files without downloading pinned upstream revisions.",
    )
    args = parser.parse_args()

    for file_name, metadata in OFFICIAL_SOURCES.items():
        path = DOCS_DIR / file_name
        content = (
            path.read_text(encoding="utf-8")
            if args.local_only
            else download_text(str(metadata["raw"]))
        )
        cleaned = clean_snapshot(content)
        attribution = (
            "<!-- AutoOnCall retrieval snapshot\n"
            f"Upstream: {metadata['source']}\n"
            f"Upstream revision: {metadata['revision']}\n"
            f"Retrieved: {SNAPSHOT_DATE}\n"
            f"License: {metadata['license']}\n"
            "Transformation: front matter, comments, shortcodes, internal-link wrappers, "
            "and generic navigation text removed\n"
            "-->\n\n"
        )
        path.write_text(attribution + cleaned.strip() + "\n", encoding="utf-8")
        print(f"Refreshed {file_name} at {metadata['revision'][:12]}")


def download_text(url: str) -> str:
    result = subprocess.run(
        [
            "curl.exe",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            "90",
            "--retry",
            "3",
            "--retry-all-errors",
            url,
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8")


def clean_snapshot(content: str) -> str:
    text = content.replace("\r\n", "\n").lstrip("\ufeff")
    text = re.sub(r"\A---\n.*?\n---\n+", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^\[//\]:\s*#.*$", "", text, flags=re.MULTILINE)
    text = re.sub(
        r"\[\[([^\]]+)\]\(\{\{<\s*relref\s+\"[^\"]+\"\s*>\}\}\)\]\(/[^)]+\)",
        r"\1",
        text,
    )
    text = re.sub(r"\{\{[%<].*?[%>]\}\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\(\{\{<\s*relref\s+\"([^\"]+)\"\s*>\}\}\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(/[^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((?:\.\./)+[^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)#]+\.md)(#[^)]+)?\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(/[^)]+\)", r"\1", text)
    text = re.sub(
        r"(?im)^>\s*For a curated documentation index,.*(?:\n|$)",
        "",
        text,
    )
    text = re.sub(r"(?im)^Before you begin, ensure you have the following:\s*$", "", text)
    text = re.sub(
        r"(?im)^- Access to Grafana Loki logs and metrics\s*$|"
        r"^- Understanding of .* basics\s*$|"
        r"^- Permissions to configure limits and settings if needed\s*$",
        "",
        text,
    )
    text = normalize_setext_headings(text)
    text = re.sub(r"\[([^\]]+)\]\(\)", r"\1", text)
    text = re.sub(r"(?im)^## Related resources\s*$.*\Z", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^#{1,6}\s*$\n?", "", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def normalize_setext_headings(text: str) -> str:
    lines = text.splitlines()
    normalized: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and re.fullmatch(r"={3,}\s*", lines[index + 1]):
            normalized.append(f"# {lines[index].strip()}")
            index += 2
            continue
        if index + 1 < len(lines) and re.fullmatch(r"-{3,}\s*", lines[index + 1]):
            normalized.append(f"## {lines[index].strip()}")
            index += 2
            continue
        normalized.append(lines[index])
        index += 1
    return "\n".join(normalized)


if __name__ == "__main__":
    main()
