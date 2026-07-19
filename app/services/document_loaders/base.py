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
    quality_content: str | None = Field(default=None, exclude=True, repr=False)
    deduplication_content: str | None = Field(default=None, exclude=True, repr=False)


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
            self.add_warning(warning)

    def add_warning(self, warning: str) -> None:
        """Record a bounded warning list so large dirty files cannot inflate reports."""
        if len(self.warnings) < MAX_CLEANING_WARNINGS - 1:
            self.warnings.append(str(warning)[:300])
        elif len(self.warnings) == MAX_CLEANING_WARNINGS - 1:
            self.warnings.append("additional cleaning warnings omitted")

    def extend_warnings(self, warnings: list[str]) -> None:
        for warning in warnings:
            self.add_warning(warning)


class DocumentLoader(Protocol):
    """Source-specific loader contract."""

    loader_type: str
    supported_extensions: set[str]

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        """Load a file into logical text units plus a cleaning report."""
        ...


MIN_EFFECTIVE_CHARS = 30
MAX_CLEANING_WARNINGS = 200


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
    return str(value).replace("\x00", "").strip()


def has_enough_information(text: str, *, min_chars: int = MIN_EFFECTIVE_CHARS) -> bool:
    """Return True when a unit has enough letters or digits to index."""
    useful = "".join(char for char in text if char.isalnum())
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
        quality_content = normalize_text(
            unit.quality_content if unit.quality_content is not None else content
        )
        if not has_enough_information(quality_content, min_chars=min_chars):
            report.record_drop("low_information", warning=f"unit {index} too short")
            continue
        deduplication_content = normalize_text(
            unit.deduplication_content if unit.deduplication_content is not None else content
        )
        digest = hashlib.sha256(deduplication_content.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            report.record_drop("duplicate", warning=f"unit {index} duplicate")
            continue
        seen_hashes.add(digest)
        accepted.append(unit.model_copy(update={"content": content}))
        report.record_indexed()

    return accepted, report
