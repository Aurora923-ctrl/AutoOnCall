"""Tests for knowledge indexing quality reports."""

from __future__ import annotations

import io
import json

import pytest
from fastapi import UploadFile

from app.api import file as file_api
from app.services.indexing_quality_service import (
    IndexingQualityService,
    build_indexing_quality_report,
)
from app.services.vector_index_service import IndexingResult, SingleFileIndexingResult


def test_quality_service_persists_single_file_cleaning_report(tmp_path) -> None:
    service = IndexingQualityService(tmp_path / "quality.jsonl")
    result = SingleFileIndexingResult(
        file_path=str(tmp_path / "scanned.pdf"),
        status="empty",
        chunk_count=0,
        message="empty scanned pdf",
        cleaning_report={
            "source_file": "scanned.pdf",
            "loader_type": "pdf",
            "raw_units": 2,
            "indexed_units": 0,
            "dropped_units": 2,
            "empty_units": 2,
            "duplicate_units": 0,
            "low_information_units": 0,
            "warnings": ["PDF 未提取到有效文本，扫描件需要 OCR 后再入库"],
        },
    ).finish()

    record = service.record_single_file_result(result, operation="upload")
    report = service.build_report()

    assert record.doc_type == "pdf"
    assert report["summary"]["total_raw_units"] == 2
    assert report["summary"]["total_empty_units"] == 2
    assert report["summary"]["warning_file_count"] == 1
    assert report["by_doc_type"][0]["doc_type"] == "pdf"
    assert report["low_quality_files"][0]["source_file"] == "scanned.pdf"
    assert "OCR" in report["low_quality_files"][0]["warnings"][0]


def test_quality_service_aggregates_by_doc_type(tmp_path) -> None:
    service = IndexingQualityService(tmp_path / "quality.jsonl")
    for source_file, loader_type, status, dropped in [
        ("runbook.md", "plain_text", "success", 0),
        ("wiki.html", "html", "success", 1),
        ("tickets.csv", "table", "success", 2),
    ]:
        service.record_single_file_result(
            SingleFileIndexingResult(
                file_path=str(tmp_path / source_file),
                status=status,
                chunk_count=1,
                cleaning_report={
                    "source_file": source_file,
                    "loader_type": loader_type,
                    "raw_units": 3,
                    "indexed_units": 2,
                    "dropped_units": dropped,
                    "empty_units": dropped,
                    "duplicate_units": 0,
                    "low_information_units": 0,
                    "warnings": [],
                },
            ).finish(),
            operation="directory",
        )

    report = service.build_report()
    by_type = {item["doc_type"]: item for item in report["by_doc_type"]}

    assert by_type["markdown"]["file_count"] == 1
    assert by_type["html"]["dropped_units"] == 1
    assert by_type["table"]["empty_units"] == 2
    assert len(service.list_records(doc_type="table")) == 1


def test_quality_service_records_directory_failures(tmp_path) -> None:
    service = IndexingQualityService(tmp_path / "quality.jsonl")
    result = IndexingResult()
    result.start_time = result.end_time = None
    result.failed_files[str(tmp_path / "broken.pdf")] = "xref table missing"

    records = service.record_directory_result(result)

    assert records[0].status == "failed"
    assert records[0].source_file == "broken.pdf"
    assert records[0].doc_type == "pdf"
    assert service.build_report()["summary"]["failed_file_count"] == 1


@pytest.mark.asyncio
async def test_upload_records_indexing_quality(monkeypatch, tmp_path) -> None:
    quality = IndexingQualityService(tmp_path / "quality.jsonl")
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(file_api, "indexing_quality_service", quality)

    def empty_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="empty",
            chunk_count=0,
            message="empty table",
            cleaning_report={
                "source_file": "tickets.csv",
                "loader_type": "table",
                "raw_units": 2,
                "indexed_units": 0,
                "dropped_units": 2,
                "empty_units": 2,
                "duplicate_units": 0,
                "low_information_units": 0,
                "warnings": ["row 2 empty"],
            },
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", empty_index)

    upload = UploadFile(file=io.BytesIO(b"a,b\n,\n"), filename="tickets.csv")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["data"]["indexing"]["cleaning"]["empty_units"] == 2
    report = quality.build_report()
    assert report["summary"]["empty_file_count"] == 1
    assert report["by_doc_type"][0]["doc_type"] == "table"


@pytest.mark.asyncio
async def test_knowledge_indexing_reports_api_filters_doc_type(monkeypatch, tmp_path) -> None:
    quality = IndexingQualityService(tmp_path / "quality.jsonl")
    quality.record_single_file_result(
        SingleFileIndexingResult(
            file_path=str(tmp_path / "wiki.html"),
            status="success",
            chunk_count=1,
            cleaning_report={
                "source_file": "wiki.html",
                "loader_type": "html",
                "raw_units": 1,
                "indexed_units": 1,
                "dropped_units": 0,
                "empty_units": 0,
                "duplicate_units": 0,
                "low_information_units": 0,
                "warnings": [],
            },
        ).finish(),
        operation="upload",
    )
    quality.record_single_file_result(
        SingleFileIndexingResult(
            file_path=str(tmp_path / "tickets.xlsx"),
            status="success",
            chunk_count=1,
            cleaning_report={
                "source_file": "tickets.xlsx",
                "loader_type": "table",
                "raw_units": 1,
                "indexed_units": 1,
                "dropped_units": 0,
                "empty_units": 0,
                "duplicate_units": 0,
                "low_information_units": 0,
                "warnings": [],
            },
        ).finish(),
        operation="upload",
    )
    monkeypatch.setattr(file_api, "indexing_quality_service", quality)

    response = await file_api.knowledge_indexing_reports(doc_type="table", limit=10)

    assert response["code"] == 200
    assert response["data"]["summary"]["total_records"] == 1
    assert response["data"]["items"][0]["source_file"] == "tickets.xlsx"
    assert response["data"]["by_doc_type"][0]["doc_type"] == "table"


def test_build_indexing_quality_report_flags_low_quality_files() -> None:
    report = build_indexing_quality_report([])
    assert report["summary"]["total_records"] == 0
    assert report["by_doc_type"] == []
    assert report["low_quality_files"] == []

