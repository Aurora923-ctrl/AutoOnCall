"""Tests for knowledge indexing quality reports."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import UploadFile

from app.api import file as file_api
from app.services.indexing_quality_service import (
    IndexingQualityRecord,
    IndexingQualityService,
    build_indexing_quality_report,
    build_quality_record,
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
    assert records[0].error_message == "索引失败，请检查服务端日志"
    assert "xref" not in records[0].error_message
    assert service.build_report()["summary"]["failed_file_count"] == 1


def test_quality_service_serializes_concurrent_jsonl_writes(tmp_path) -> None:
    storage_path = tmp_path / "quality.jsonl"
    services = [IndexingQualityService(storage_path) for _ in range(4)]

    def record(index: int) -> None:
        services[index % len(services)].record_failed_file(
            source_path=str(tmp_path / f"broken-{index}.pdf"),
            operation="upload",
            error_message="索引失败，请检查服务端日志",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(100)))

    lines = storage_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert all(json.loads(line)["status"] == "failed" for line in lines)
    assert len(IndexingQualityService(storage_path).list_records(limit=500)) == 100


def test_quality_service_serializes_cross_process_jsonl_writes(tmp_path) -> None:
    storage_path = tmp_path / "quality.jsonl"
    worker = """
import sys

from app.services.indexing_quality_service import IndexingQualityService

storage_path, prefix = sys.argv[1:3]
service = IndexingQualityService(storage_path)
for index in range(50):
    service.record_failed_file(
        source_path=f"{prefix}-{index}.pdf",
        operation="upload",
        error_message="indexing failed",
    )
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(storage_path), prefix],
            cwd=tmp_path,
        )
        for prefix in ("worker-a", "worker-b")
    ]

    assert [process.wait(timeout=30) for process in processes] == [0, 0]
    lines = storage_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]

    assert len(records) == 100
    assert len({record["source_file"] for record in records}) == 100
    assert all(record["status"] == "failed" for record in records)


def test_quality_service_creates_missing_parent_before_locking(tmp_path) -> None:
    storage_path = tmp_path / "nested" / "reports" / "quality.jsonl"
    service = IndexingQualityService(storage_path)

    service.record_failed_file(
        source_path=str(tmp_path / "broken.pdf"),
        operation="upload",
        error_message="xref table missing at C:/private/path",
    )

    record = service.list_records()[0]
    assert storage_path.exists()
    assert record.error_message == "索引失败，请检查服务端日志"
    assert "private" not in record.error_message


def test_quality_service_warns_when_corrupt_lines_are_skipped(monkeypatch, tmp_path) -> None:
    storage_path = tmp_path / "quality.jsonl"
    service = IndexingQualityService(storage_path)
    service.record_failed_file(
        source_path=str(tmp_path / "broken.pdf"),
        operation="upload",
        error_message="indexing failed",
    )
    with storage_path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
        handle.write("{}\n")

    warnings: list[str] = []

    def capture_warning(message: str, *args: object) -> None:
        warnings.append(message.format(*args))

    monkeypatch.setattr(
        "app.services.indexing_quality_service.logger.warning",
        capture_warning,
    )

    records = service.list_records()

    assert len(records) == 1
    assert len(warnings) == 1
    assert "file=quality.jsonl" in warnings[0]
    assert "invalid_count=2" in warnings[0]
    assert "lines=[2, 3]" in warnings[0]
    assert "{broken" not in warnings[0]


def test_quality_service_skips_invalid_utf8_lines_without_losing_valid_records(
    monkeypatch,
    tmp_path,
) -> None:
    storage = tmp_path / "quality.jsonl"
    valid = IndexingQualityRecord(source_file="runbook.md", status="success")
    storage.write_bytes(
        valid.model_dump_json().encode("utf-8") + b"\n" + b'{"source_file":"broken-\xff.md"}\n'
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        "app.services.indexing_quality_service.logger.warning",
        lambda message, *args: warnings.append(message.format(*args)),
    )

    records = IndexingQualityService(storage).list_records()

    assert [record.source_file for record in records] == ["runbook.md"]
    assert warnings
    assert "invalid_count=1" in warnings[0]


def test_quality_service_sanitizes_failed_result_objects_and_windows_paths(tmp_path) -> None:
    service = IndexingQualityService(tmp_path / "quality.jsonl")
    result = SingleFileIndexingResult(
        file_path=r"C:\private\uploads\broken.pdf",
        status="failed",
        chunk_count=0,
        error_message=r"xref missing at C:\private\uploads\broken.pdf",
    ).finish()

    record = service.record_single_file_result(result, operation="upload")

    assert record.source_path == "broken.pdf"
    assert record.error_message == "索引失败，请检查服务端日志"
    assert "private" not in record.model_dump_json()


@pytest.mark.parametrize(
    ("warnings", "expected"),
    [
        ("single warning", ["single warning"]),
        ({"detail": "bad source"}, ["{'detail': 'bad source'}"]),
    ],
)
def test_quality_service_normalizes_malformed_warning_collections(
    warnings,
    expected,
) -> None:
    record = build_quality_record(
        report={"warnings": warnings},
        source_path="runbook.md",
        operation="upload",
        status="empty",
    )

    assert record.warnings == expected


def test_quality_record_normalizes_malformed_loader_fields() -> None:
    record = build_quality_record(
        report={
            "source_file": r"C:\private\uploads\runbook.md",
            "raw_units": "invalid",
            "indexed_units": -2,
            "dropped_units": None,
            "warnings": [f"warning-{index}" for index in range(1000)],
        },
        source_path=r"C:\private\uploads\runbook.md",
        operation="upload",
        status="unexpected",
        chunk_count=-3,
        duration_ms=-5,
    )

    assert record.source_file == "runbook.md"
    assert record.source_path == "runbook.md"
    assert record.status == "unknown"
    assert record.chunk_count == 0
    assert record.duration_ms == 0
    assert record.raw_units == 0
    assert record.indexed_units == 0
    assert len(record.warnings) == 200
    assert "private" not in record.model_dump_json()


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
