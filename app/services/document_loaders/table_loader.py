"""CSV and XLSX loaders for exported tickets, SLOs, and dependency tables."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.services.document_loaders.base import (
    DocumentCleaningReport,
    LoadedDocument,
    base_metadata,
    filter_loaded_documents,
    normalize_text,
)

MAX_CELL_CHARS = 500
MAX_COLUMNS = 24
PRIMARY_KEY_FIELDS = ("ticket_id", "incident_id", "service_name", "error_code", "id")


class TableDocumentLoader:
    """Load CSV/XLSX rows as row-level auditable knowledge units."""

    loader_type = "table"
    supported_extensions = {"csv", "xlsx"}

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        suffix = path.suffix.lower().removeprefix(".")
        raw_units, warnings = _load_csv(path) if suffix == "csv" else _load_xlsx(path)
        documents, report = filter_loaded_documents(
            path,
            loader_type=self.loader_type,
            raw_units=raw_units,
        )
        report.warnings.extend(warnings)
        return documents, report


def _load_csv(path: Path) -> tuple[list[LoadedDocument], list[str]]:
    rows = _read_csv_rows(path)
    warnings: list[str] = []
    documents = [
        _row_to_document(
            path,
            row=row,
            row_number=index + 2,
            sheet_name="csv",
            table_name=path.stem,
            warnings=warnings,
        )
        for index, row in enumerate(rows)
    ]
    return documents, warnings


def _load_xlsx(path: Path) -> tuple[list[LoadedDocument], list[str]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        documents: list[LoadedDocument] = []
        warnings: list[str] = []
        for worksheet in workbook.worksheets:
            rows = worksheet.iter_rows(values_only=True)
            headers = [_clean_header(value) for value in next(rows, [])]
            if not any(headers):
                continue
            for row_index, values in enumerate(rows, 2):
                row = {
                    header: value for header, value in zip(headers, values, strict=False) if header
                }
                documents.append(
                    _row_to_document(
                        path,
                        row=row,
                        row_number=row_index,
                        sheet_name=worksheet.title,
                        table_name=worksheet.title,
                        warnings=warnings,
                    )
                )
        return documents, warnings
    finally:
        workbook.close()


def _row_to_document(
    path: Path,
    *,
    row: dict[str, Any],
    row_number: int,
    sheet_name: str,
    table_name: str,
    warnings: list[str],
) -> LoadedDocument:
    clean_items = []
    row_items = list(row.items())
    if len(row_items) > MAX_COLUMNS:
        warnings.append(f"sheet={sheet_name} row={row_number} ignored columns after {MAX_COLUMNS}")
    for key, value in row_items[:MAX_COLUMNS]:
        text = normalize_text(value)
        if not text:
            continue
        if len(text) > MAX_CELL_CHARS:
            warnings.append(
                f"sheet={sheet_name} row={row_number} cell {key} truncated from {len(text)} chars"
            )
            text = text[:MAX_CELL_CHARS] + "...[truncated]"
        clean_items.append((str(key), text))
    if not clean_items:
        return LoadedDocument(
            content="",
            metadata=base_metadata(path, doc_type="table")
            | {
                "sheet_name": sheet_name,
                "row_number": row_number,
                "table_name": table_name,
                "primary_key": "",
            },
        )
    primary_key = _primary_key(clean_items)
    lines = [
        f"表格: {path.name}",
        f"sheet: {sheet_name}",
        f"row: {row_number}",
    ]
    if primary_key:
        lines.append(f"primary_key: {primary_key}")
    lines.extend(f"{key}: {value}" for key, value in clean_items)
    return LoadedDocument(
        content="\n".join(lines),
        metadata=base_metadata(path, doc_type="table")
        | {
            "sheet_name": sheet_name,
            "row_number": row_number,
            "table_name": table_name,
            "primary_key": primary_key,
        },
    )


def _clean_header(value: Any) -> str:
    return normalize_text(value).strip().replace(" ", "_").lower()


def _primary_key(items: list[tuple[str, str]]) -> str:
    values = {key.lower(): value for key, value in items}
    for field in PRIMARY_KEY_FIELDS:
        if values.get(field):
            return f"{field}={values[field]}"
    return ""


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """Read CSV rows with encodings common in exported Chinese ticket systems."""
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return list(csv.DictReader(handle))
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return []
