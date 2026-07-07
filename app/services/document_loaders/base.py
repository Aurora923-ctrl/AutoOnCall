"""Shared contracts for source-specific document loading."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


class LoadedDocument(BaseModel):
    """Text plus metadata extracted from one logical source unit."""

    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentCleaningReport(BaseModel):
    """Audit summary for loader-level cleaning and filtering."""

    source_file: str
    loader_type: str
    raw_units: int = 0
    indexed_units: int = 0
    dropped_units: int = 0
    empty_units: int = 0
    duplicate_units: int = 0
    low_information_units: int = 0
    warnings: list[str] = Field(default_factory=list)

    def record_indexed(self) -> None:
        self.indexed_units += 1

    def record_drop(self, reason: str, *, warning: str = "") -> None:
        self.dropped_units += 1
        if reason == "empty":
            self.empty_units += 1
        elif reason == "duplicate":
            self.duplicate_units += 1
        elif reason == "low_information":
            self.low_information_units += 1
        if warning:
            self.warnings.append(warning[:300])


class DocumentLoader(Protocol):
    """Source-specific loader contract."""

    loader_type: str
    supported_extensions: set[str]

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        """Load a file into logical text units plus a cleaning report."""
        ...


MIN_EFFECTIVE_CHARS = 30


def base_metadata(path: Path, *, doc_type: str) -> dict[str, Any]:
    """Return metadata shared by all loader outputs."""
    return {
        "_source": path.resolve().as_posix(),
        "_extension": path.suffix.lower(),
        "_file_name": path.name,
        "_doc_id": path.resolve().as_posix(),
        "doc_type": doc_type,
    }


def normalize_text(value: Any) -> str:
    """Normalize text extracted from rich sources without changing meaning."""
    if value is None:
        return ""
    lines = [line.strip() for line in str(value).replace("\x00", "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def has_enough_information(text: str, *, min_chars: int = MIN_EFFECTIVE_CHARS) -> bool:
    """Return True when a unit has enough non-space text to index."""
    useful = "".join(char for char in text if not char.isspace())
    return len(useful) >= min_chars


def filter_loaded_documents(
    path: Path,
    *,
    loader_type: str,
    raw_units: list[LoadedDocument],
    min_chars: int = MIN_EFFECTIVE_CHARS,
) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
    """Apply shared empty/low-information/duplicate filtering."""
    report = DocumentCleaningReport(source_file=path.name, loader_type=loader_type)
    report.raw_units = len(raw_units)
    seen_hashes: set[str] = set()
    accepted: list[LoadedDocument] = []

    for index, unit in enumerate(raw_units, 1):
        content = normalize_text(unit.content)
        if not content:
            report.record_drop("empty", warning=f"unit {index} empty")
            continue
        if not has_enough_information(content, min_chars=min_chars):
            report.record_drop("low_information", warning=f"unit {index} too short")
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            report.record_drop("duplicate", warning=f"unit {index} duplicate")
            continue
        seen_hashes.add(digest)
        accepted.append(unit.model_copy(update={"content": content}))
        report.record_indexed()

    return accepted, report
