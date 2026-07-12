"""Repository documentation and runtime-reference integrity tests."""

from pathlib import Path

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
        "memory_high_usage.md",
        "mysql_slow_query_postmortem.pdf",
        "payment_wiki.html",
        "redis_capacity_wiki.html",
        "redis_postmortem.pdf",
        "service_unavailable.md",
        "slow_response.md",
        "tickets.csv",
        "tickets.xlsx",
    ]
    assert set(required_names).issubset(names)
    assert all(name.isascii() for name in names)
    assert all(name.isascii() for name in names)
