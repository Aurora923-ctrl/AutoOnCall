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
        """Convert a value to text and keep at most ``limit`` source characters."""
        text = "" if value is None else str(value)
        max_chars = self._normalize_limit(limit)
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}{self.budget.truncation_marker}"

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

    def _normalize_limit(self, limit: int | None) -> int:
        if limit is None:
            return max(0, self.budget.default_chars)
        return max(0, limit)


DEFAULT_CONTEXT_BUDGETER = ContextBudgeter()
