"""Focused tests for the split retrieval metadata module."""

from app.services.rag_retrieval.metadata import (
    build_milvus_metadata_expr,
    metadata_matches_filter,
    normalize_metadata_filter,
)


def test_metadata_module_normalizes_and_builds_milvus_expression() -> None:
    metadata_filter = {
        "_document_version": "v2",
        "service": ["billing", "payment"],
        "": "ignored",
    }

    assert normalize_metadata_filter(metadata_filter) == {
        "_document_version": "v2",
        "service": ["billing", "payment"],
    }
    assert build_milvus_metadata_expr(metadata_filter) == (
        'metadata["_document_version"] == "v2" and '
        'metadata["service"] in ["billing", "payment"]'
    )


def test_metadata_module_preserves_scalar_types_during_post_filter() -> None:
    assert metadata_matches_filter({"enabled": True}, {"enabled": True})
    assert not metadata_matches_filter({"enabled": True}, {"enabled": 1})
