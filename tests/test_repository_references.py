"""Repository documentation and runtime-reference integrity tests."""

import hashlib
import re
from pathlib import Path

from scripts.data.update_official_snapshot_manifest import update_official_snapshot_manifest
from scripts.maintenance.verify_references import find_reference_issues

ROOT = Path(__file__).resolve().parents[1]


def test_repository_markdown_links_and_runtime_paths_are_valid() -> None:
    issues = find_reference_issues(ROOT)
    assert issues == [], "\n".join(
        f"{item.source}:{item.line}: {item.reason}: {item.reference}" for item in issues
    )


def test_knowledge_base_has_stable_ascii_asset_names() -> None:
    knowledge_base = ROOT / "docs" / "knowledge-base"
    names = sorted(path.name for path in knowledge_base.iterdir() if path.is_file())

    required_names = [
        "cpu_high_usage.md",
        "disk_high_usage.md",
        "dns_resolution_failure_runbook.md",
        "kubernetes_scheduling_failure_runbook.md",
        "memory_high_usage.md",
        "message_queue_backlog_runbook.md",
        "mysql_lock_wait_runbook.md",
        "mysql_slow_query_postmortem.pdf",
        "network_timeout_runbook.md",
        "payment_wiki.html",
        "redis_capacity_wiki.html",
        "redis_postmortem.pdf",
        "service_unavailable.md",
        "slow_response.md",
        "tickets.xlsx",
        "thread_pool_exhaustion_runbook.md",
        "tls_certificate_expiry_runbook.md",
    ]
    assert set(required_names).issubset(names)
    assert all(name.isascii() for name in names)
    assert "tickets.csv" not in names


def test_official_snapshot_manifest_matches_current_files() -> None:
    manifest = (ROOT / "docs" / "knowledge-base-official-sources.md").read_text(
        encoding="utf-8"
    )
    hash_rows = dict(
        re.findall(
            r"^\| `([^`]+)` \| `([A-F0-9]{64})` \|$",
            manifest,
            flags=re.MULTILINE,
        )
    )
    official_files = sorted(
        path.name for path in (ROOT / "docs" / "knowledge-base").glob("official_*.md")
    )

    assert sorted(hash_rows) == official_files
    for file_name, expected_hash in hash_rows.items():
        actual_hash = hashlib.sha256(
            (ROOT / "docs" / "knowledge-base" / file_name).read_bytes()
        ).hexdigest().upper()
        assert actual_hash == expected_hash


def test_official_snapshot_manifest_updater_is_deterministic(tmp_path: Path) -> None:
    docs_dir = tmp_path / "knowledge-base"
    docs_dir.mkdir()
    (docs_dir / "official_z.md").write_text("z snapshot\n", encoding="utf-8")
    (docs_dir / "official_a.md").write_text("a snapshot\n", encoding="utf-8")
    manifest_path = tmp_path / "official-sources.md"
    prefix = (
        b"\xef\xbb\xbf# Sources\n\n"
        b"Keep this prefix byte-for-byte.\n\n"
        b"## Current cleaned hashes\r\n\r\n"
    )
    suffix = b"## Distribution boundary\n\n- Preserve attribution.\r\n"
    stale_table = (
        b"| Local file | SHA-256 |\r\n"
        b"| --- | --- |\r\n"
        + b"| `stale.md` | `"
        + b"0" * 64
        + b"` |\r\n\r\n"
    )
    manifest_path.write_bytes(prefix + stale_table + suffix)

    update_official_snapshot_manifest(manifest_path=manifest_path, docs_dir=docs_dir)
    first_render = manifest_path.read_bytes()
    update_official_snapshot_manifest(manifest_path=manifest_path, docs_dir=docs_dir)

    assert manifest_path.read_bytes() == first_render
    assert first_render.startswith(prefix)
    assert first_render.endswith(suffix)
    assert first_render.index(b"official_a.md") < first_render.index(b"official_z.md")
    for path in sorted(docs_dir.glob("official_*.md")):
        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        expected_row = f"| `{path.name}` | `{expected_hash}` |".encode()
        assert expected_row in first_render
