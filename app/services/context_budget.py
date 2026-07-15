"""Shared helpers for bounding prompt context before LLM calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

TRUNCATION_MARKER = "\n...<truncated>"


@dataclass(frozen=True)
class ContextBudget:
    """Character budgets for prompt sections.

    The project still budgets by characters instead of model-specific tokens so
    offline tests and non-LLM paths stay deterministic.
    """

    default_chars: int = 3000
    raw_alert_chars: int = 4000
    json_indent: int = 2
    truncation_marker: str = TRUNCATION_MARKER


class ContextBudgeter:
    """Serialize and trim prompt sections with one consistent truncation marker."""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def text(self, value: Any, *, limit: int | None = None) -> str:
        """Convert a value to text and keep the rendered output within ``limit`` characters."""
        text = "" if value is None else str(value)
        max_chars = self._normalize_limit(limit)
        if len(text) <= max_chars:
            return text
        marker = self.budget.truncation_marker
        if max_chars <= 0:
            return ""
        if len(marker) >= max_chars:
            return marker[:max_chars]
        return f"{text[: max_chars - len(marker)]}{marker}"

    def json(
        self,
        value: Any,
        *,
        limit: int | None = None,
        sort_keys: bool = False,
    ) -> str:
        """Serialize a value as JSON where possible, then apply the text budget."""
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                default=str,
                indent=self.budget.json_indent,
                sort_keys=sort_keys,
            )
        except TypeError:
            text = str(value)
        return self.text(text, limit=limit)

    def sections(
        self,
        values: list[str],
        *,
        limit: int | None = None,
        separator: str = "\n\n",
    ) -> list[str]:
        """Keep complete ordered sections within one shared character budget."""
        if not isinstance(separator, str):
            raise TypeError("separator 必须是字符串")
        max_chars = self._normalize_limit(limit)
        selected: list[str] = []
        used_chars = 0
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            separator_chars = len(separator) if selected else 0
            if used_chars + separator_chars + len(text) > max_chars:
                break
            selected.append(text)
            used_chars += separator_chars + len(text)
        return selected

    def limit(self, value: int | None = None) -> int:
        """Return one validated non-negative character limit."""
        return self._normalize_limit(value)

    def _normalize_limit(self, limit: int | None) -> int:
        if limit is None:
            return max(0, self.budget.default_chars)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是整数")
        if limit < 0:
            raise ValueError("limit 不能为负数")
        return limit


DEFAULT_CONTEXT_BUDGETER = ContextBudgeter()
