"""Tests for exact RAG asset-to-index identity checks."""

from scripts.eval.rag_index_identity import assess_index_identity, assess_lexical_identity


def test_index_identity_requires_exact_content_and_chunk_match() -> None:
    expected = [
        {
            "id": "vec-1",
            "source_file": "redis.md",
            "source_id": "docs/knowledge-base/redis.md",
            "chunk_id": "redis.md#0001",
            "document_hash": "doc",
            "chunk_hash": "new",
        }
    ]
    actual = [{**expected[0], "chunk_hash": "old"}]

    result = assess_index_identity(expected, actual)

    assert result["status"] == "failed"
    assert result["identity_mismatches"][0]["fields"]["chunk_hash"] == {
        "expected": "new",
        "actual": "old",
    }


def test_index_identity_rejects_missing_and_unexpected_rows() -> None:
    expected = [{"id": "expected"}]
    actual = [{"id": "legacy"}]

    result = assess_index_identity(expected, actual)

    assert result["status"] == "failed"
    assert result["missing_ids"] == ["expected"]
    assert result["unexpected_ids"] == ["legacy"]


def test_lexical_identity_requires_exact_chunk_metadata() -> None:
    expected = [
        {
            "source_file": "redis.md",
            "source_id": "docs/knowledge-base/redis.md",
            "chunk_id": "redis.md#0001",
            "document_hash": "doc",
            "chunk_hash": "new",
        }
    ]

    result = assess_lexical_identity(expected, [{**expected[0], "chunk_hash": "old"}])

    assert result["status"] == "failed"
    assert result["missing_records"]
    assert result["unexpected_records"]
