"""Persistent read model for knowledge indexing and loader cleaning quality."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from loguru import logger
from pydantic import BaseModel, Field

from app.config import config
from app.models.incident import new_model_id, utc_now
from app.services.document_loaders.base import MAX_CLEANING_WARNINGS


class IndexingQualityRecord(BaseModel):
    """One file-level indexing quality snapshot."""

    record_id: str = Field(default_factory=lambda: new_model_id("idxq"))
    source_file: str
    source_path: str = ""
    operation: str = "upload"
    status: str = "unknown"
    chunk_count: int = 0
    duration_ms: int = 0
    doc_type: str = ""
    loader_type: str = ""
    raw_units: int = 0
    indexed_units: int = 0
    dropped_units: int = 0
    empty_units: int = 0
    duplicate_units: int = 0
    low_information_units: int = 0
    warnings: list[str] = Field(default_factory=list)
    message: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class IndexingQualityService:
    """Store and query loader cleaning reports as an observable quality view."""

    def __init__(self, storage_path: str | Path | None = None) -> None:
        self.storage_path = Path(storage_path or config.knowledge_indexing_report_path)
        self.lock_path = self.storage_path.with_name(f".{self.storage_path.name}.lock")

    def record_single_file_result(
        self,
        result: Any,
        *,
        operation: str,
        source_path: str | None = None,
    ) -> IndexingQualityRecord:
        """Persist the quality report for one indexed file result."""
        record = build_quality_record_from_result(
            result,
            operation=operation,
            source_path=source_path,
        )
        self._append(record)
        return record

    def record_directory_result(
        self, result: Any, *, operation: str = "directory"
    ) -> list[IndexingQualityRecord]:
        """Persist all file-level cleaning reports from one directory indexing run."""
        records: list[IndexingQualityRecord] = []
        cleaning_reports = getattr(result, "cleaning_reports", {}) or {}
        success_by_path = {
            str(item.get("file_path") or ""): item
            for item in getattr(result, "success_files", []) or []
            if isinstance(item, dict)
        }
        empty_files = getattr(result, "empty_files", {}) or {}
        failed_files = getattr(result, "failed_files", {}) or {}

        for path, report in cleaning_reports.items():
            file_info = success_by_path.get(path, {})
            status = (
                "success"
                if path in success_by_path
                else "empty"
                if path in empty_files
                else "unknown"
            )
            record = build_quality_record(
                report=report,
                source_path=path,
                operation=operation,
                status=status,
                chunk_count=int(file_info.get("chunk_count") or 0),
                duration_ms=getattr(result, "get_duration_ms", lambda: 0)(),
                message=str(file_info.get("message") or empty_files.get(path) or ""),
            )
            self._append(record)
            records.append(record)

        for path, error in failed_files.items():
            record = build_quality_record(
                report={},
                source_path=path,
                operation=operation,
                status="failed",
                error_message=_public_indexing_error_message(str(error)),
                duration_ms=getattr(result, "get_duration_ms", lambda: 0)(),
            )
            self._append(record)
            records.append(record)
        return records

    def record_failed_file(
        self,
        *,
        source_path: str,
        operation: str,
        error_message: str,
    ) -> IndexingQualityRecord:
        """Persist a public-safe failed indexing quality record."""
        record = build_quality_record(
            report={},
            source_path=source_path,
            operation=operation,
            status="failed",
            error_message=_public_indexing_error_message(error_message),
        )
        self._append(record)
        return record

    def list_records(
        self,
        *,
        doc_type: str | None = None,
        limit: int = 100,
    ) -> list[IndexingQualityRecord]:
        """Return recent quality records, optionally filtered by document type."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.lock_path)):
            records = _read_records(self.storage_path)
        if doc_type:
            records = [record for record in records if record.doc_type == doc_type]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[: max(1, min(limit, 500))]

    def build_report(self, *, doc_type: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Build an API-facing quality report with aggregate stats."""
        records = self.list_records(doc_type=doc_type, limit=limit)
        return build_indexing_quality_report(records)

    def _append(self, record: IndexingQualityRecord) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.lock_path)):
            with self.storage_path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")


def build_quality_record_from_result(
    result: Any,
    *,
    operation: str,
    source_path: str | None = None,
) -> IndexingQualityRecord:
    """Create a quality record from SingleFileIndexingResult-like objects."""
    report = _safe_report(getattr(result, "cleaning_report", {}))
    resolved_source_path = source_path or getattr(result, "file_path", "")
    return build_quality_record(
        report=report,
        source_path=str(resolved_source_path or ""),
        operation=operation,
        status=str(getattr(result, "status", "unknown") or "unknown"),
        chunk_count=int(getattr(result, "chunk_count", 0) or 0),
        duration_ms=int(getattr(result, "get_duration_ms", lambda: 0)() or 0),
        message=str(getattr(result, "message", "") or ""),
        error_message=str(getattr(result, "error_message", "") or ""),
    )


def build_quality_record(
    *,
    report: dict[str, Any],
    source_path: str,
    operation: str,
    status: str,
    chunk_count: int = 0,
    duration_ms: int = 0,
    message: str = "",
    error_message: str = "",
) -> IndexingQualityRecord:
    """Normalize raw cleaning report dictionaries into the persistent schema."""
    report = _safe_report(report)
    source_file = _public_path(str(report.get("source_file") or source_path)) or "unknown"
    doc_type = _doc_type_from_report(report, source_file)
    normalized_status = _normalize_quality_status(status)
    safe_error_message = (
        _public_indexing_error_message(error_message)
        if normalized_status == "failed"
        else error_message
    )
    return IndexingQualityRecord(
        source_file=source_file,
        source_path=_public_path(source_path),
        operation=operation,
        status=normalized_status,
        chunk_count=max(_safe_non_negative_int(chunk_count), 0),
        duration_ms=max(_safe_non_negative_int(duration_ms), 0),
        doc_type=doc_type,
        loader_type=str(report.get("loader_type") or ""),
        raw_units=_safe_non_negative_int(report.get("raw_units")),
        indexed_units=_safe_non_negative_int(report.get("indexed_units")),
        dropped_units=_safe_non_negative_int(report.get("dropped_units")),
        empty_units=_safe_non_negative_int(report.get("empty_units")),
        duplicate_units=_safe_non_negative_int(report.get("duplicate_units")),
        low_information_units=_safe_non_negative_int(report.get("low_information_units")),
        warnings=_normalize_warnings(report.get("warnings")),
        message=message,
        error_message=safe_error_message,
    )


def build_indexing_quality_report(records: list[IndexingQualityRecord]) -> dict[str, Any]:
    """Aggregate records by doc_type and expose low-quality files."""
    summary = {
        "total_records": len(records),
        "total_raw_units": sum(record.raw_units for record in records),
        "total_indexed_units": sum(record.indexed_units for record in records),
        "total_dropped_units": sum(record.dropped_units for record in records),
        "total_empty_units": sum(record.empty_units for record in records),
        "total_duplicate_units": sum(record.duplicate_units for record in records),
        "total_low_information_units": sum(record.low_information_units for record in records),
        "warning_file_count": sum(1 for record in records if record.warnings),
        "empty_file_count": sum(1 for record in records if record.status == "empty"),
        "failed_file_count": sum(1 for record in records if record.status == "failed"),
    }
    by_doc_type: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record.doc_type or "unknown"
        bucket = by_doc_type.setdefault(
            key,
            {
                "doc_type": key,
                "file_count": 0,
                "raw_units": 0,
                "indexed_units": 0,
                "dropped_units": 0,
                "empty_units": 0,
                "duplicate_units": 0,
                "low_information_units": 0,
                "warning_file_count": 0,
                "empty_file_count": 0,
                "failed_file_count": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["raw_units"] += record.raw_units
        bucket["indexed_units"] += record.indexed_units
        bucket["dropped_units"] += record.dropped_units
        bucket["empty_units"] += record.empty_units
        bucket["duplicate_units"] += record.duplicate_units
        bucket["low_information_units"] += record.low_information_units
        bucket["warning_file_count"] += 1 if record.warnings else 0
        bucket["empty_file_count"] += 1 if record.status == "empty" else 0
        bucket["failed_file_count"] += 1 if record.status == "failed" else 0

    low_quality_files = [
        record.model_dump(mode="json")
        for record in records
        if record.status in {"empty", "failed"}
        or record.warnings
        or record.dropped_units
        or record.empty_units
        or record.low_information_units
    ]
    return {
        "summary": summary,
        "by_doc_type": sorted(by_doc_type.values(), key=lambda item: str(item["doc_type"])),
        "low_quality_files": low_quality_files,
        "items": [record.model_dump(mode="json") for record in records],
    }


def _doc_type_from_report(report: dict[str, Any], source_file: str) -> str:
    loader_type = str(report.get("loader_type") or "")
    if loader_type == "plain_text":
        suffix = Path(source_file).suffix.lower()
        return "markdown" if suffix in {".md", ".markdown"} else "text"
    if loader_type == "table":
        return "table"
    if loader_type:
        return loader_type
    suffix = Path(source_file).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".csv", ".xlsx"}:
        return "table"
    return suffix.removeprefix(".") or "unknown"


def _public_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def _public_indexing_error_message(value: str) -> str:
    return "索引失败，请检查服务端日志" if str(value or "") else ""


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _normalize_quality_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"success", "empty", "failed"} else "unknown"


def _safe_report(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_warnings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    return [str(item)[:300] for item in values[:MAX_CLEANING_WARNINGS]]


def _read_records(path: Path) -> list[IndexingQualityRecord]:
    if not path.exists():
        return []
    records: list[IndexingQualityRecord] = []
    invalid_line_numbers: list[int] = []
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    line = raw_line.decode("utf-8")
                    records.append(IndexingQualityRecord.model_validate(json.loads(line)))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    invalid_line_numbers.append(line_number)
    except OSError as exc:
        raise RuntimeError(f"读取索引质量记录失败: {path.name}") from exc
    if invalid_line_numbers:
        logger.warning(
            "索引质量记录包含无效行，已跳过: file={}, invalid_count={}, lines={}",
            path.name,
            len(invalid_line_numbers),
            invalid_line_numbers[:20],
        )
    return records


indexing_quality_service = IndexingQualityService()
