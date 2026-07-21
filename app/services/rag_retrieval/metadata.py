"""Metadata-filter normalization and Milvus expression construction."""

from __future__ import annotations

import math
import re
from typing import Any

from loguru import logger

METADATA_FILTER_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def metadata_matches_filter(metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    """Apply a small equality/inclusion metadata filter for non-Milvus test stores."""
    for key, expected in normalize_metadata_filter(metadata_filter).items():
        actual = metadata.get(key)
        if isinstance(expected, list):
            if not any(_metadata_values_equal(actual, item) for item in expected):
                return False
        elif not _metadata_values_equal(actual, expected):
            return False
    return True


def _metadata_values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return float(actual) == float(expected)
    return type(actual) is type(expected) and actual == expected


def build_milvus_metadata_expr(metadata_filter: dict[str, Any] | None) -> str | None:
    """Build a Milvus JSON metadata expression for exact-match filters."""
    normalized = normalize_metadata_filter(metadata_filter)
    if not normalized:
        return None
    expressions = []
    for key, value in normalized.items():
        metadata_key = key if key.startswith("_") else key
        if isinstance(value, list):
            values = ", ".join(_quote_expr_value(item) for item in value)
            expressions.append(f'metadata["{metadata_key}"] in [{values}]')
        else:
            expressions.append(f'metadata["{metadata_key}"] == {_quote_expr_value(value)}')
    return " and ".join(expressions)


def normalize_metadata_filter(
    metadata_filter: dict[str, Any] | None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Drop empty filter values while preserving exact-match semantics."""
    if not metadata_filter:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in metadata_filter.items():
        safe_key = str(key).strip()
        if not safe_key:
            if strict:
                raise ValueError("metadata filter key 不能为空")
            continue
        if not METADATA_FILTER_KEY_PATTERN.fullmatch(safe_key):
            if strict:
                raise ValueError(f"非法 metadata filter key: {safe_key}")
            logger.warning(f"忽略非法 metadata filter key: {safe_key}")
            continue
        if value in (None, ""):
            if strict:
                raise ValueError(f"metadata filter value 不能为空: {safe_key}")
            continue
        if isinstance(value, list):
            if strict and any(
                item in (None, "") or not _is_metadata_scalar(item) for item in value
            ):
                raise ValueError(f"metadata filter list 不能为空且只能包含标量: {safe_key}")
            items = [item for item in value if item not in (None, "") and _is_metadata_scalar(item)]
            if items:
                normalized[safe_key] = items
            elif strict:
                raise ValueError(f"metadata filter list 不能为空且只能包含标量: {safe_key}")
        elif _is_metadata_scalar(value):
            normalized[safe_key] = value
        elif strict:
            raise ValueError(f"metadata filter value 只能是标量或标量列表: {safe_key}")
        else:
            logger.warning(f"忽略非法 metadata filter value: key={safe_key}")
    return normalized


def _quote_expr_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_metadata_scalar(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str | int | bool)
