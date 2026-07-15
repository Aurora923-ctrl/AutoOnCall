"""File API boundary behavior tests."""

import asyncio
import io
import json
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from app.api import file as file_api
from app.services import vector_index_service as vector_index_module
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_index_service import SingleFileIndexingResult, VectorIndexService


@pytest.mark.asyncio
async def test_upload_config_returns_backend_constraints(monkeypatch) -> None:
    monkeypatch.setattr(file_api, "ALLOWED_EXTENSIONS", ["txt", "md"])
    monkeypatch.setattr(file_api, "MAX_FILE_SIZE", 3 * 1024 * 1024)
    monkeypatch.setattr(file_api, "MAX_FILE_SIZE_MB", 3)

    payload = await file_api.upload_config()

    assert payload == {
        "code": 200,
        "message": "success",
        "data": {
            "allowed_extensions": ["txt", "md"],
            "max_file_size": 3 * 1024 * 1024,
            "max_file_size_mb": 3,
        },
    }


@pytest.mark.asyncio
async def test_upload_file_reports_indexing_failure_without_failing_upload(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    called_paths: list[str] = []

    def fail_index(path: str) -> None:
        called_paths.append(path)
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", fail_index)

    upload = UploadFile(file=io.BytesIO(b"# runbook\n"), filename="runbook.md")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["code"] == 207
    assert payload["message"] == "partial_success"
    assert payload["data"]["filename"] == "runbook.md"
    assert payload["data"]["file_path"] == "runbook.md"
    assert str(tmp_path) not in payload["data"]["file_path"]
    assert payload["data"]["overwritten"] is False
    assert payload["data"]["indexing_ready"] is False
    assert payload["data"]["indexing"] == {
        "status": "failed",
        "chunk_count": 0,
        "duration_ms": 0,
        "error_message": file_api.PUBLIC_INDEXING_ERROR_MESSAGE,
        "message": "文件已保存，但索引未完成",
        "cleaning": {},
    }
    assert (tmp_path / "runbook.md").read_bytes() == b"# runbook\n"
    assert called_paths == [str(tmp_path / "runbook.md")]


@pytest.mark.asyncio
async def test_upload_file_closes_input_stream_after_response(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        file_api.vector_index_service,
        "index_single_file",
        lambda path: SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish(),
    )
    upload = UploadFile(
        file=io.BytesIO(b"# runbook\nvalid diagnostic content"),
        filename="runbook.md",
    )

    response = await file_api.upload_file(upload)

    assert response.status_code == 200
    assert upload.file.closed is True


@pytest.mark.asyncio
async def test_upload_file_closes_input_stream_after_validation_failure() -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename="../runbook.md")

    with pytest.raises(HTTPException):
        await file_api.upload_file(upload)

    assert upload.file.closed is True


@pytest.mark.asyncio
async def test_upload_corrupt_pdf_reports_public_safe_partial_success(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def fail_index(path: str) -> None:
        raise RuntimeError(f"PDF parse failed at {path}: xref table missing")

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", fail_index)

    upload = UploadFile(file=io.BytesIO(b"%PDF-1.7 corrupt"), filename="broken.pdf")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["message"] == "partial_success"
    assert payload["data"]["filename"] == "broken.pdf"
    assert payload["data"]["file_path"] == "broken.pdf"
    assert payload["data"]["indexing_ready"] is False
    assert payload["data"]["indexing"]["status"] == "failed"
    assert payload["data"]["indexing"]["error_message"] == file_api.PUBLIC_INDEXING_ERROR_MESSAGE
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_upload_file_reports_empty_indexing_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def empty_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="empty",
            chunk_count=0,
            message="文件内容为空或无法切分，未写入向量索引",
        )

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", empty_index)

    upload = UploadFile(file=io.BytesIO(b""), filename="empty.md")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["message"] == "partial_success"
    assert payload["data"]["indexing_ready"] is False
    assert payload["data"]["indexing"]["status"] == "empty"
    assert payload["data"]["indexing"]["chunk_count"] == 0
    assert "duration_ms" in payload["data"]["indexing"]
    assert "未写入向量索引" in payload["data"]["indexing"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "indexing_result",
    [
        None,
        {"status": "success", "chunk_count": 0},
        {"status": "unknown", "chunk_count": 1},
    ],
)
async def test_upload_file_fails_closed_for_invalid_indexing_results(
    monkeypatch,
    tmp_path,
    indexing_result,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        file_api.vector_index_service,
        "index_single_file",
        lambda _path: indexing_result,
    )

    upload = UploadFile(file=io.BytesIO(b"# runbook\nvalid content"), filename="runbook.md")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["message"] == "partial_success"
    assert payload["data"]["indexing_ready"] is False
    assert payload["data"]["indexing"]["status"] == "failed"
    assert payload["data"]["indexing"]["chunk_count"] == 0
    assert payload["data"]["indexing"]["error_message"] == file_api.PUBLIC_INDEXING_ERROR_MESSAGE


@pytest.mark.asyncio
async def test_quality_report_failure_does_not_downgrade_successful_indexing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    class FailingQualityService:
        def record_single_file_result(self, *args, **kwargs) -> None:
            raise OSError("quality storage unavailable")

        def record_failed_file(self, *args, **kwargs) -> None:
            raise AssertionError("successful indexing must not be recorded as failed")

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)
    monkeypatch.setattr(file_api, "indexing_quality_service", FailingQualityService())

    upload = UploadFile(file=io.BytesIO(b"# runbook\nvalid content"), filename="runbook.md")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["indexing_ready"] is True
    assert payload["data"]["indexing"]["status"] == "success"
    assert payload["data"]["indexing"]["chunk_count"] == 1


@pytest.mark.asyncio
async def test_upload_file_accepts_markdown_extension_and_reports_overwrite(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    existing = tmp_path / "guide.markdown"
    existing.write_text("old", encoding="utf-8")

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=2,
            message="文件索引完成",
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)

    upload = UploadFile(file=io.BytesIO(b"# guide\nnew"), filename="guide.markdown")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["filename"] == "guide.markdown"
    assert payload["data"]["overwritten"] is True
    assert payload["data"]["indexing_ready"] is True
    assert payload["data"]["indexing"]["status"] == "success"
    assert payload["data"]["indexing"]["chunk_count"] == 2
    assert (tmp_path / "guide.markdown").read_bytes() == b"# guide\nnew"


@pytest.mark.asyncio
async def test_concurrent_same_name_uploads_index_their_own_saved_version(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    first_index_started = threading.Event()
    release_first_index = threading.Event()
    invocation_lock = threading.Lock()
    invocation_count = 0
    indexed_contents: list[bytes] = []

    class NoopQualityService:
        def record_single_file_result(self, *args, **kwargs) -> None:
            return None

        def record_failed_file(self, *args, **kwargs) -> None:
            return None

    def success_index(path: str) -> SingleFileIndexingResult:
        nonlocal invocation_count
        with invocation_lock:
            invocation_count += 1
            invocation = invocation_count
        if invocation == 1:
            first_index_started.set()
            assert release_first_index.wait(timeout=5)
        indexed_contents.append(Path(path).read_bytes())
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)
    monkeypatch.setattr(file_api, "indexing_quality_service", NoopQualityService())

    first_task = asyncio.create_task(
        file_api.upload_file(UploadFile(file=io.BytesIO(b"first-version"), filename="same.md"))
    )
    assert await asyncio.to_thread(first_index_started.wait, 5)

    second_task = asyncio.create_task(
        file_api.upload_file(UploadFile(file=io.BytesIO(b"second-version"), filename="same.md"))
    )
    await asyncio.sleep(0.05)
    assert not second_task.done()

    release_first_index.set()
    first_response, second_response = await asyncio.gather(first_task, second_task)
    first_payload = json.loads(first_response.body.decode("utf-8"))
    second_payload = json.loads(second_response.body.decode("utf-8"))

    assert indexed_contents == [b"first-version", b"second-version"]
    assert (tmp_path / "same.md").read_bytes() == b"second-version"
    assert first_payload["data"]["overwritten"] is False
    assert second_payload["data"]["overwritten"] is True
    assert first_payload["data"]["indexing_ready"] is True
    assert second_payload["data"]["indexing_ready"] is True


@pytest.mark.asyncio
async def test_cancelled_upload_keeps_same_name_lock_until_indexing_thread_finishes(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    first_index_started = threading.Event()
    release_first_index = threading.Event()
    invocation_lock = threading.Lock()
    invocation_count = 0
    indexed_contents: list[bytes] = []

    class NoopQualityService:
        def record_single_file_result(self, *args, **kwargs) -> None:
            return None

        def record_failed_file(self, *args, **kwargs) -> None:
            return None

    def success_index(path: str) -> SingleFileIndexingResult:
        nonlocal invocation_count
        with invocation_lock:
            invocation_count += 1
            invocation = invocation_count
        if invocation == 1:
            first_index_started.set()
            assert release_first_index.wait(timeout=5)
        indexed_contents.append(Path(path).read_bytes())
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)
    monkeypatch.setattr(file_api, "indexing_quality_service", NoopQualityService())

    first_task = asyncio.create_task(
        file_api.upload_file(UploadFile(file=io.BytesIO(b"first-version"), filename="same.md"))
    )
    assert await asyncio.to_thread(first_index_started.wait, 5)
    first_task.cancel()

    second_task = asyncio.create_task(
        file_api.upload_file(UploadFile(file=io.BytesIO(b"second-version"), filename="same.md"))
    )
    await asyncio.sleep(0.05)
    assert not first_task.done()
    assert not second_task.done()

    release_first_index.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    second_response = await second_task
    second_payload = json.loads(second_response.body.decode("utf-8"))

    assert indexed_contents == [b"first-version", b"second-version"]
    assert (tmp_path / "same.md").read_bytes() == b"second-version"
    assert second_payload["data"]["overwritten"] is True
    assert second_payload["data"]["indexing_ready"] is True


@pytest.mark.asyncio
async def test_cancelled_streaming_upload_removes_temporary_file(tmp_path) -> None:
    target = tmp_path / "runbook.md"
    second_read_started = asyncio.Event()

    class BlockingUpload:
        def __init__(self) -> None:
            self.read_count = 0

        async def read(self, _size: int) -> bytes:
            self.read_count += 1
            if self.read_count == 1:
                return b"partial-content"
            second_read_started.set()
            await asyncio.Event().wait()
            return b""

    task = asyncio.create_task(file_api._save_upload_file(BlockingUpload(), target))
    await second_read_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_upload_file_rejects_filename_without_useful_basename() -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename=".md")

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "文件名不能为空"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../runbook.md", "a/b.md", "a:b.md"])
async def test_upload_file_rejects_filename_rewriting_collisions(filename: str) -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename=filename)

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "文件名" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_upload_file_rejects_non_normalized_unicode_filename() -> None:
    upload = UploadFile(
        file=io.BytesIO(b"content"),
        filename="cafe\u0301.md",
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "NFC Unicode" in str(exc_info.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["bidi\u202efdp.md", "zero\u200bwidth.md"])
async def test_upload_file_rejects_invisible_or_directional_filename_controls(
    filename: str,
) -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename=filename)

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "不允许的字符" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_upload_file_preserves_safe_spaces_in_filename(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)

    upload = UploadFile(file=io.BytesIO(b"# valid runbook content"), filename="redis guide.md")
    response = await file_api.upload_file(upload)
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["data"]["filename"] == "redis guide.md"
    assert (tmp_path / "redis guide.md").exists()


@pytest.mark.asyncio
async def test_upload_file_rejects_utf8_filename_that_exceeds_platform_byte_budget() -> None:
    filename = f"{'知' * 74}.md"
    upload = UploadFile(file=io.BytesIO(b"content"), filename=filename)

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "UTF-8 编码" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_upload_file_rejects_explicit_mime_extension_mismatch() -> None:
    upload = UploadFile(
        file=io.BytesIO(b"%PDF-1.7"),
        filename="runbook.md",
        headers={"content-type": "application/pdf"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "MIME 类型" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_upload_file_accepts_generic_mime_for_cli_upload(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)

    upload = UploadFile(
        file=io.BytesIO(b"# runbook\nvalid content"),
        filename="runbook.md",
        headers={"content-type": "application/octet-stream"},
    )
    response = await file_api.upload_file(upload)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_upload_rejects_invalid_signature_without_overwriting_existing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    existing = tmp_path / "runbook.pdf"
    existing.write_bytes(b"%PDF-1.7 existing")

    upload = UploadFile(
        file=io.BytesIO(b"not a pdf"),
        filename="runbook.pdf",
        headers={"content-type": "application/pdf"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "PDF 文件签名" in str(exc_info.value.detail)
    assert existing.read_bytes() == b"%PDF-1.7 existing"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_upload_rejects_binary_content_when_null_byte_is_after_prefix(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    upload = UploadFile(
        file=io.BytesIO((b"a" * 2048) + b"\x00binary"),
        filename="runbook.md",
        headers={"content-type": "text/markdown"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "二进制内容" in str(exc_info.value.detail)
    assert not (tmp_path / "runbook.md").exists()


@pytest.mark.asyncio
async def test_upload_allows_pdf_signature_text_inside_markdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)

    response = await file_api.upload_file(
        UploadFile(
            file=io.BytesIO(
                b"# PDF troubleshooting\n\nExample file header: %PDF-1.7 with diagnostic context."
            ),
            filename="pdf-troubleshooting.md",
            headers={"content-type": "text/markdown"},
        )
    )

    assert response.status_code == 200
    assert (tmp_path / "pdf-troubleshooting.md").exists()


@pytest.mark.asyncio
async def test_upload_removes_abandoned_same_name_temp_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    abandoned = tmp_path / ".runbook.md.abandoned.tmp"
    abandoned.write_bytes(b"partial")

    def success_index(path: str) -> SingleFileIndexingResult:
        return SingleFileIndexingResult(
            file_path=path,
            status="success",
            chunk_count=1,
        ).finish()

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", success_index)

    response = await file_api.upload_file(
        UploadFile(file=io.BytesIO(b"# valid runbook content"), filename="runbook.md")
    )

    assert response.status_code == 200
    assert not abandoned.exists()


@pytest.mark.asyncio
async def test_upload_file_rejects_reserved_or_too_long_filename() -> None:
    reserved = UploadFile(file=io.BytesIO(b"content"), filename="CON.md")
    with pytest.raises(HTTPException) as reserved_exc:
        await file_api.upload_file(reserved)
    assert reserved_exc.value.status_code == 400
    assert "系统保留名称" in str(reserved_exc.value.detail)

    long_name = f"{'a' * (file_api.MAX_SAFE_FILENAME_LENGTH + 1)}.md"
    too_long = UploadFile(file=io.BytesIO(b"content"), filename=long_name)
    with pytest.raises(HTTPException) as long_exc:
        await file_api.upload_file(too_long)
    assert long_exc.value.status_code == 400
    assert "文件名过长" in str(long_exc.value.detail)


@pytest.mark.asyncio
async def test_upload_file_rejects_reserved_device_name_with_multiple_extensions() -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename="CON.txt.md")

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "系统保留名称" in str(exc_info.value.detail)


def test_cleanup_stale_upload_temps_handles_glob_characters_literally(tmp_path) -> None:
    target = tmp_path / "run[1].md"
    stale = tmp_path / ".run[1].md.abandoned.tmp"
    unrelated = tmp_path / ".run1.md.abandoned.tmp"
    stale.write_bytes(b"partial")
    unrelated.write_bytes(b"keep")

    file_api._cleanup_stale_upload_temps(target)

    assert not stale.exists()
    assert unrelated.exists()


@pytest.mark.asyncio
async def test_upload_file_rejects_oversized_file_without_overwriting_existing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(file_api, "MAX_FILE_SIZE", 4)
    existing = tmp_path / "guide.md"
    existing.write_bytes(b"safe")
    index_called = False

    def fail_if_called(path: str) -> None:
        nonlocal index_called
        index_called = True

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", fail_if_called)

    upload = UploadFile(file=io.BytesIO(b"too-large"), filename="guide.md")

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert "文件大小超过限制" in str(exc_info.value.detail)
    assert existing.read_bytes() == b"safe"
    assert index_called is False
    assert not list(tmp_path.glob("*.tmp"))


def test_index_directory_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    service = VectorIndexService()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(allowed))

    service._ensure_directory_allowed(allowed.resolve())
    with pytest.raises(ValueError, match="目录不在允许索引范围内"):
        service._ensure_directory_allowed(outside.resolve())


@pytest.mark.asyncio
async def test_index_directory_api_returns_403_for_disallowed_directory(
    monkeypatch,
    tmp_path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    service = VectorIndexService()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(allowed))
    monkeypatch.setattr(file_api, "vector_index_service", service)

    response = await file_api.index_directory(str(outside))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 403
    assert payload["code"] == 403
    assert payload["message"] == "partial_success"
    assert payload["data"]["error_type"] == "forbidden_directory"
    assert payload["data"]["error_message"] == "目录不在允许索引范围内"
    assert str(outside.resolve()) not in payload["data"]["error_message"]
    assert "allowed_roots" not in payload["data"]["error_message"]


@pytest.mark.asyncio
async def test_index_directory_api_returns_400_for_missing_allowed_directory(
    monkeypatch,
    tmp_path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    missing = allowed / "missing"
    service = VectorIndexService()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(allowed))
    monkeypatch.setattr(file_api, "vector_index_service", service)

    response = await file_api.index_directory(str(missing))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 400
    assert payload["code"] == 400
    assert payload["message"] == "partial_success"
    assert payload["data"]["error_type"] == "invalid_directory"
    assert payload["data"]["error_message"] == "目录不存在或不是有效目录"


@pytest.mark.asyncio
async def test_index_directory_api_hides_paths_and_raw_file_errors(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "runbook.md"
    runbook.write_text("# Redis", encoding="utf-8")
    service = VectorIndexService()

    def fail_index(path: str) -> SingleFileIndexingResult:
        raise RuntimeError(f"milvus unavailable at {path}")

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(service, "index_single_file", fail_index)
    monkeypatch.setattr(file_api, "vector_index_service", service)

    response = await file_api.index_directory(str(docs_dir))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 207
    assert payload["data"]["directory_path"] == "docs"
    assert payload["data"]["directory_name"] == "docs"
    assert payload["data"]["failed_files"] == {
        "runbook.md": "索引失败，请检查服务端日志",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "milvus unavailable" not in serialized


@pytest.mark.asyncio
async def test_index_directory_quality_failure_does_not_hide_indexing_result(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    class SuccessfulResult:
        success = True

        def to_public_dict(self) -> dict:
            return {
                "success": True,
                "directory_path": "docs",
                "success_count": 1,
                "fail_count": 0,
                "empty_count": 0,
            }

    class FailingQualityService:
        def record_directory_result(self, *args, **kwargs) -> None:
            raise OSError("quality storage unavailable")

    monkeypatch.setattr(
        file_api.vector_index_service,
        "index_directory",
        lambda _path: SuccessfulResult(),
    )
    monkeypatch.setattr(file_api, "indexing_quality_service", FailingQualityService())

    response = await file_api.index_directory(str(docs_dir))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["message"] == "success"
    assert payload["data"]["success_count"] == 1


def test_index_single_file_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    outside_file = outside / "runbook.md"
    outside_file.write_text("# outside", encoding="utf-8")
    service = VectorIndexService()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(allowed))

    with pytest.raises(ValueError, match="文件不在允许索引范围内"):
        service.index_single_file(str(outside_file))


def test_index_directory_includes_markdown_extension(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    for filename in [
        "guide.txt",
        "runbook.md",
        "postmortem.markdown",
        "wiki.html",
        "tickets.csv",
    ]:
        (docs_dir / filename).write_text("content", encoding="utf-8")
    indexed_files: list[str] = []
    service = VectorIndexService()

    def fake_index(path: str) -> SingleFileIndexingResult:
        indexed_files.append(path)
        return SingleFileIndexingResult(file_path=path, status="success", chunk_count=1).finish()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(service, "index_single_file", fake_index)

    result = service.index_directory(str(docs_dir))

    assert result.total_files == 5
    assert result.success_count == 5
    assert {Path(path).name for path in indexed_files} == {
        "guide.txt",
        "runbook.md",
        "postmortem.markdown",
        "wiki.html",
        "tickets.csv",
    }


def test_index_directory_defaults_to_configured_upload_dir(monkeypatch, tmp_path) -> None:
    upload_dir = tmp_path / "custom-uploads"
    upload_dir.mkdir()
    (upload_dir / "guide.md").write_text("# Guide", encoding="utf-8")
    indexed_files: list[str] = []

    monkeypatch.setattr(vector_index_module.config, "upload_dir", str(upload_dir))
    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", "")
    service = VectorIndexService()

    def fake_index(path: str) -> SingleFileIndexingResult:
        indexed_files.append(path)
        return SingleFileIndexingResult(file_path=path, status="success", chunk_count=1).finish()

    monkeypatch.setattr(service, "index_single_file", fake_index)

    result = service.index_directory()

    assert result.success is True
    assert result.directory_path == str(upload_dir.resolve())
    assert [Path(path).name for path in indexed_files] == ["guide.md"]


def test_splitter_adds_document_version_metadata() -> None:
    docs = document_splitter_service.split_document(
        "# Redis\n\nRedis timeout runbook", "docs/knowledge-base/redis.md"
    )

    assert docs
    metadata = docs[0].metadata
    assert metadata["_document_version"]
    assert len(metadata["_document_hash"]) == 64
    assert len(metadata["_chunk_hash"]) == 64
    assert metadata["_version_key"].startswith("redis.md:")


def test_splitter_treats_markdown_extension_as_markdown() -> None:
    docs = document_splitter_service.split_document(
        "# Redis\n\nRedis timeout runbook",
        "docs/knowledge-base/redis.markdown",
    )

    assert docs
    assert docs[0].metadata["_extension"] == ".markdown"
    assert docs[0].metadata["_file_name"] == "redis.markdown"
    assert docs[0].metadata.get("h1") == "Redis"


def test_splitter_treats_uppercase_markdown_extension_as_markdown() -> None:
    docs = document_splitter_service.split_document(
        "# Redis\n\nRedis timeout runbook",
        "docs/knowledge-base/REDIS.MD",
    )

    assert docs
    assert docs[0].metadata["_extension"] == ".md"
    assert docs[0].metadata.get("h1") == "Redis"


def test_splitter_does_not_merge_chunks_across_markdown_headings() -> None:
    docs = document_splitter_service.split_document(
        "# Redis\n\n## maxclients\n\n连接数耗尽处理。\n\n## latency\n\n延迟升高处理。",
        "docs/knowledge-base/redis.md",
    )

    assert len(docs) == 2
    assert [doc.metadata.get("h2") for doc in docs] == ["maxclients", "latency"]
    assert "latency" not in docs[0].page_content


def test_splitter_merge_keeps_overlap_separate_and_respects_chunk_limit() -> None:
    docs = document_splitter_service.split_document(
        "# Runbook\n\n" + ("A" * 1700),
        "docs/knowledge-base/large.md",
    )
    content_docs = [doc for doc in docs if "A" in doc.page_content]

    assert len(content_docs) == 2
    assert all(len(doc.page_content) <= document_splitter_service.chunk_size * 2 for doc in docs)
    assert content_docs[0].page_content[-100:] == content_docs[1].page_content[:100]


def test_index_single_file_marks_existing_index_stale_when_vector_add_fails(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("# Redis\n\nRedis maxclients timeout runbook", encoding="utf-8")
    service = VectorIndexService()

    class FailingVectorStoreManager:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []
            self.deleted_batches: list[tuple[str, list[str]]] = []

        def add_documents(self, documents):
            raise RuntimeError("milvus unavailable")

        def delete_by_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 0

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            self.deleted_batches.append((source, vector_ids))
            return 0

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []
            self.upserted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []
            self.events: list[tuple[str, str]] = []

        def delete_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 0

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            self.upserted_sources.append(source)
            self.events.append(("upsert", source))

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))
            self.events.append(("stale", reason))

        def clear_source_stale(self, source: str) -> None:
            self.events.append(("clear", source))

    fake_vector = FailingVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    with pytest.raises(RuntimeError, match="索引文件失败"):
        service.index_single_file(str(runbook))

    assert fake_vector.deleted_sources == []
    assert fake_vector.deleted_batches == []
    assert fake_lexical.deleted_sources == []
    assert fake_lexical.upserted_sources == []
    assert fake_lexical.stale_sources[-1] == (
        runbook.resolve().as_posix(),
        "milvus unavailable",
    )


def test_index_single_file_cleans_non_current_vector_ids_after_success(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("# Redis\n\nRedis maxclients timeout runbook", encoding="utf-8")
    service = VectorIndexService()

    class RecordingVectorStoreManager:
        def __init__(self) -> None:
            self.added_count = 0
            self.cleaned_batches: list[tuple[str, list[str]]] = []

        def add_documents(self, documents):
            self.added_count = len(documents)
            return [f"vec-{index}" for index, _ in enumerate(documents, 1)]

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            self.cleaned_batches.append((source, vector_ids))
            return 4

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.upserted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []
            self.cleared_sources: list[str] = []

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            self.upserted_sources.append(source)

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

        def clear_source_stale(self, source: str) -> None:
            self.cleared_sources.append(source)

    fake_vector = RecordingVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    result = service.index_single_file(str(runbook))
    normalized_path = runbook.resolve().as_posix()

    assert result.status == "success"
    assert fake_vector.added_count > 0
    assert fake_vector.cleaned_batches == [
        (normalized_path, [f"vec-{index}" for index in range(1, fake_vector.added_count + 1)])
    ]
    assert fake_lexical.upserted_sources == [normalized_path]
    assert fake_lexical.stale_sources == [(normalized_path, "indexing_in_progress")]
    assert fake_lexical.cleared_sources == [normalized_path]


def test_index_single_file_keeps_source_stale_until_vector_cleanup_finishes(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("# Redis\n\nRedis maxclients timeout runbook", encoding="utf-8")
    service = VectorIndexService()
    events: list[str] = []

    class RecordingVectorStoreManager:
        def add_documents(self, documents):
            events.append("vector_upsert")
            return [f"vec-{index}" for index, _ in enumerate(documents, 1)]

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            events.append("vector_cleanup")
            return 0

    class RecordingLexicalIndex:
        def mark_source_stale(self, source: str, reason: str) -> None:
            events.append("stale")

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            assert clear_stale is False
            events.append("lexical_replace")

        def clear_source_stale(self, source: str) -> None:
            events.append("clear_stale")

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(
        vector_index_module,
        "vector_store_manager",
        RecordingVectorStoreManager(),
    )
    monkeypatch.setattr(
        vector_index_module,
        "lexical_index_service",
        RecordingLexicalIndex(),
    )

    result = service.index_single_file(str(runbook))

    assert result.status == "success"
    assert events == [
        "stale",
        "vector_upsert",
        "lexical_replace",
        "vector_cleanup",
        "clear_stale",
    ]


def test_index_single_file_marks_source_stale_when_vector_cleanup_fails(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("# Redis\n\nRedis maxclients timeout runbook", encoding="utf-8")
    service = VectorIndexService()

    class FailingCleanupVectorStoreManager:
        def __init__(self) -> None:
            self.compensated_ids: list[list[str]] = []

        def add_documents(self, documents):
            return [f"vec-{index}" for index, _ in enumerate(documents, 1)]

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            raise RuntimeError("cleanup unavailable")

        def delete_by_ids(self, vector_ids: list[str], *, raise_on_error: bool = False) -> int:
            self.compensated_ids.append(vector_ids)
            return len(vector_ids)

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.upserted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            self.upserted_sources.append(source)

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

        def clear_source_stale(self, source: str) -> None:
            raise AssertionError(source)

    fake_vector = FailingCleanupVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(
        vector_index_module,
        "vector_store_manager",
        fake_vector,
    )
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    with pytest.raises(RuntimeError, match="索引文件失败"):
        service.index_single_file(str(runbook))

    normalized_path = runbook.resolve().as_posix()
    assert fake_lexical.upserted_sources == [normalized_path]
    assert fake_vector.compensated_ids
    assert fake_lexical.stale_sources[-1] == (normalized_path, "cleanup unavailable")


def test_index_single_file_compensates_new_vectors_when_lexical_commit_fails(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("# Redis\n\nRedis maxclients timeout runbook", encoding="utf-8")
    service = VectorIndexService()

    class RecordingVectorStoreManager:
        def __init__(self) -> None:
            self.added_ids = ["vec-new"]
            self.compensated_ids: list[list[str]] = []

        def add_documents(self, documents):
            return self.added_ids

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            return 0

        def delete_by_ids(self, vector_ids: list[str], *, raise_on_error: bool = False) -> int:
            self.compensated_ids.append(vector_ids)
            return len(vector_ids)

    class FailingLexicalIndex:
        def __init__(self) -> None:
            self.stale_sources: list[tuple[str, str]] = []

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            raise OSError("lexical disk unavailable")

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

    fake_vector = RecordingVectorStoreManager()
    fake_lexical = FailingLexicalIndex()
    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    with pytest.raises(RuntimeError, match="索引文件失败"):
        service.index_single_file(str(runbook))

    assert fake_vector.compensated_ids == [["vec-new"]]
    assert fake_lexical.stale_sources[-1][1] == "lexical disk unavailable"


def test_index_single_file_empty_content_clears_existing_indexes(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("   \n", encoding="utf-8")
    service = VectorIndexService()

    class RecordingVectorStoreManager:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []

        def delete_by_source(self, source: str, *, raise_on_error: bool = False) -> int:
            self.deleted_sources.append(source)
            return 3

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []

        def delete_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 2

    fake_vector = RecordingVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    result = service.index_single_file(str(runbook))
    normalized_path = runbook.resolve().as_posix()

    assert result.status == "empty"
    assert fake_vector.deleted_sources == [normalized_path]
    assert fake_lexical.deleted_sources == [normalized_path]
    assert "已清理旧索引" in (result.message or "")


def test_index_single_file_empty_content_marks_source_stale_when_vector_delete_fails(
    monkeypatch,
    tmp_path,
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    runbook = docs_dir / "redis.md"
    runbook.write_text("   \n", encoding="utf-8")
    service = VectorIndexService()

    class FailingVectorStoreManager:
        def delete_by_source(self, source: str, *, raise_on_error: bool = False) -> int:
            raise RuntimeError("delete unavailable")

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []

        def delete_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 0

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", FailingVectorStoreManager())
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    with pytest.raises(RuntimeError, match="索引文件失败"):
        service.index_single_file(str(runbook))

    normalized_path = runbook.resolve().as_posix()
    assert fake_lexical.deleted_sources == []
    assert fake_lexical.stale_sources[-1] == (normalized_path, "delete unavailable")


def test_index_single_file_preserves_multi_source_loader_metadata(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    html_file = docs_dir / "wiki.html"
    html_file.write_text(
        "<h1>Payment Runbook</h1><h2>MySQL 慢查询</h2><p>使用 EXPLAIN 排查 slow query digest。</p>",
        encoding="utf-8",
    )
    csv_file = docs_dir / "tickets.csv"
    csv_file.write_text(
        "ticket_id,service_name,root_cause\n"
        "INC-REDIS-001,order-service,Redis maxclients exhausted\n",
        encoding="utf-8",
    )
    xlsx_file = docs_dir / "catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "services"
    sheet.append(["service_name", "dependency"])
    sheet.append(["payment-service", "mysql-payments"])
    workbook.save(xlsx_file)
    pdf_file = docs_dir / "postmortem.pdf"
    pdf_file.write_bytes(b"%PDF fake")

    class FakePage:
        def extract_text(self) -> str:
            return "Redis maxclients 故障复盘，connected_clients 达到 9940。"

    def fake_reader(_path: str):
        return type("FakeReader", (), {"pages": [FakePage()]})()

    class RecordingVectorStoreManager:
        def __init__(self) -> None:
            self.added_batches = []

        def add_documents(self, documents):
            self.added_batches.append(list(documents))
            return [f"vec-{index}" for index, _ in enumerate(documents, 1)]

        def delete_by_source_except_ids(
            self,
            source: str,
            vector_ids: list[str],
            *,
            raise_on_error: bool = True,
        ) -> int:
            return 0

        def delete_by_source(self, source: str, *, raise_on_error: bool = False) -> int:
            return 0

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.upserted_batches = []
            self.stale_sources: list[tuple[str, str]] = []
            self.cleared_sources: list[str] = []

        def upsert_source(self, source: str, documents, *, clear_stale: bool = True) -> None:
            self.upserted_batches.append((source, list(documents)))

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

        def clear_source_stale(self, source: str) -> None:
            self.cleared_sources.append(source)

    fake_vector = RecordingVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()
    service = VectorIndexService()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)
    monkeypatch.setattr("app.services.document_loaders.pdf_loader.PdfReader", fake_reader)

    for path in [pdf_file, html_file, csv_file, xlsx_file]:
        result = service.index_single_file(str(path))
        assert result.status == "success"
        assert result.cleaning_report["indexed_units"] >= 1

    metadata_by_name = {
        Path(batch[0].metadata["_file_name"]).name: batch[0].metadata
        for batch in fake_vector.added_batches
    }
    assert metadata_by_name["postmortem.pdf"]["page_number"] == 1
    assert metadata_by_name["wiki.html"]["heading_path"] == "Payment Runbook > MySQL 慢查询"
    assert metadata_by_name["tickets.csv"]["row_number"] == 2
    assert metadata_by_name["tickets.csv"]["primary_key"] == "ticket_id=INC-REDIS-001"
    assert metadata_by_name["catalog.xlsx"]["sheet_name"] == "services"
    assert metadata_by_name["catalog.xlsx"]["primary_key"] == "service_name=payment-service"


def test_index_single_file_empty_pdf_clears_existing_indexes(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    pdf_file = docs_dir / "scanned.pdf"
    pdf_file.write_bytes(b"%PDF fake")
    service = VectorIndexService()

    class EmptyPage:
        def extract_text(self) -> str:
            return ""

    def fake_reader(_path: str):
        return type("FakeReader", (), {"pages": [EmptyPage()]})()

    class RecordingVectorStoreManager:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []

        def delete_by_source(self, source: str, *, raise_on_error: bool = False) -> int:
            self.deleted_sources.append(source)
            return 1

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []

        def delete_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 1

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

    fake_vector = RecordingVectorStoreManager()
    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(vector_index_module, "vector_store_manager", fake_vector)
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)
    monkeypatch.setattr("app.services.document_loaders.pdf_loader.PdfReader", fake_reader)

    result = service.index_single_file(str(pdf_file))
    normalized_path = pdf_file.resolve().as_posix()

    assert result.status == "empty"
    assert result.cleaning_report["empty_units"] == 1
    assert "扫描件需要 OCR" in " ".join(result.cleaning_report["warnings"])
    assert fake_vector.deleted_sources == [normalized_path]
    assert fake_lexical.deleted_sources == [normalized_path]
