"""Tests for the knowledge quality benchmark."""

from datetime import UTC, datetime
from pathlib import Path

from scripts.eval.eval_knowledge_quality import (
    analyze_duplicates,
    asset_modified_at,
    build_quality_review,
    build_summary,
    deterministic_vector,
    parse_as_of,
)


def test_duplicate_analysis_finds_exact_duplicates() -> None:
    chunks = [
        {
            "source_file": "a.md",
            "chunk_id": "a#1",
            "length_chars": 100,
            "normalized_sha256": "same",
            "content_preview": "redis maxclients connection timeout " * 4,
        },
        {
            "source_file": "b.md",
            "chunk_id": "b#1",
            "length_chars": 100,
            "normalized_sha256": "same",
            "content_preview": "redis maxclients connection timeout " * 4,
        },
    ]

    result = analyze_duplicates(chunks, near_duplicate_threshold=0.9)

    assert result["exact_duplicate_group_count"] == 1
    assert result["exact_duplicate_chunk_count"] == 2


def test_summary_exposes_counts_and_confidence_intervals() -> None:
    assets = [
        {
            "extension": ".md",
            "parse_status": "success",
            "split_status": "success",
            "index_ready_status": "success",
            "stale": False,
            "age_days": 1.0,
            "source_file": "runbook.md",
            "errors": [],
        }
    ]
    chunks = [
        {
            "length_chars": 120,
            "empty": False,
            "overlong": False,
            "metadata_complete": True,
        }
    ]
    duplicates = {
        "exact_duplicate_chunk_count": 0,
        "near_duplicate_chunk_count": 0,
    }

    summary = build_summary(
        assets,
        chunks,
        duplicate_analysis=duplicates,
        milvus={"status": "passed", "collection_removed": True},
    )

    assert summary["status"] == "passed"
    assert summary["metrics"]["parse_success_rate"]["numerator"] == 1
    assert "confidence_interval" in summary["metrics"]["parse_success_rate"]
    assert summary["hard_gates"]["milvus_crud_consistent"] is True
    assert "overlong_chunk_rate" in summary["watch_metrics"]


def test_summary_can_pass_local_asset_checks_when_milvus_is_explicitly_skipped() -> None:
    summary = build_summary(
        [
            {
                "extension": ".md",
                "parse_status": "success",
                "split_status": "success",
                "index_ready_status": "success",
                "stale": False,
                "age_days": 1.0,
                "source_file": "runbook.md",
                "errors": [],
            }
        ],
        [
            {
                "length_chars": 120,
                "empty": False,
                "overlong": False,
                "metadata_complete": True,
            }
        ],
        duplicate_analysis={
            "exact_duplicate_chunk_count": 0,
            "near_duplicate_chunk_count": 0,
        },
        milvus={"status": "not_run", "collection_removed": False},
    )

    assert summary["status"] == "passed_without_milvus"
    assert summary["local_asset_status"] == "passed"
    assert summary["milvus_required"] is False
    assert summary["hard_gates"]["milvus_crud_consistent"] == "not_applicable"
    assert summary["hard_gates"]["milvus_cleanup_complete"] == "not_applicable"


def test_deterministic_vector_is_stable_and_normalized() -> None:
    left = deterministic_vector("Redis maxclients timeout")
    right = deterministic_vector("Redis maxclients timeout")

    assert left == right
    assert round(sum(value * value for value in left), 4) == 1.0


def test_quality_review_explains_multiformat_ticket_duplicates() -> None:
    review = build_quality_review(
        [],
        duplicate_analysis={
            "near_pairs": [
                {
                    "left": {"source_file": "tickets.csv", "chunk_id": "tickets.csv#0001"},
                    "right": {"source_file": "tickets.xlsx", "chunk_id": "tickets.xlsx#0001"},
                    "similarity": 0.97,
                }
            ]
        },
        overlong_chars=1600,
    )

    assert review["expected_duplicate_pair_count"] == 1
    assert review["review_required_count"] == 0


def test_parse_as_of_normalizes_utc() -> None:
    assert parse_as_of("2026-07-15T12:00:00+08:00") == datetime(
        2026,
        7,
        15,
        4,
        tzinfo=UTC,
    )


def test_asset_freshness_uses_git_timestamp_for_tracked_file() -> None:
    modified_at, basis = asset_modified_at(
        Path("docs/knowledge-base/cpu_high_usage.md"),
        fallback_mtime=0,
    )

    assert basis == "git_last_commit"
    assert modified_at.year >= 2025
