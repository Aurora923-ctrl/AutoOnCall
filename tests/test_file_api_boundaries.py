"""File API boundary behavior tests."""

import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

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
    assert payload["data"]["overwritten"] is False
    assert payload["data"]["indexing_ready"] is False
    assert payload["data"]["indexing"] == {
        "status": "failed",
        "chunk_count": 0,
        "duration_ms": 0,
        "error_message": file_api.PUBLIC_INDEXING_ERROR_MESSAGE,
        "message": "文件已保存，但索引未完成",
    }
    assert (tmp_path / "runbook.md").read_bytes() == b"# runbook\n"
    assert called_paths == [str(tmp_path / "runbook.md")]


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
async def test_upload_file_rejects_filename_without_useful_basename() -> None:
    upload = UploadFile(file=io.BytesIO(b"content"), filename=".md")

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "文件名不能为空"


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
    for filename in ["guide.txt", "runbook.md", "postmortem.markdown"]:
        (docs_dir / filename).write_text("content", encoding="utf-8")
    indexed_files: list[str] = []
    service = VectorIndexService()

    def fake_index(path: str) -> SingleFileIndexingResult:
        indexed_files.append(path)
        return SingleFileIndexingResult(file_path=path, status="success", chunk_count=1).finish()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(service, "index_single_file", fake_index)

    result = service.index_directory(str(docs_dir))

    assert result.total_files == 3
    assert result.success_count == 3
    assert {Path(path).name for path in indexed_files} == {
        "guide.txt",
        "runbook.md",
        "postmortem.markdown",
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
        "# Redis\n\nRedis timeout runbook", "aiops-docs/redis.md"
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
        "aiops-docs/redis.markdown",
    )

    assert docs
    assert docs[0].metadata["_extension"] == ".markdown"
    assert docs[0].metadata["_file_name"] == "redis.markdown"
    assert docs[0].metadata.get("h1") == "Redis"


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

        def delete_by_source_except_ids(self, source: str, vector_ids: list[str]) -> int:
            self.deleted_batches.append((source, vector_ids))
            return 0

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.deleted_sources: list[str] = []
            self.upserted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []

        def delete_source(self, source: str) -> int:
            self.deleted_sources.append(source)
            return 0

        def upsert_source(self, source: str, documents) -> None:
            self.upserted_sources.append(source)

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

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
    assert fake_lexical.stale_sources == [(runbook.resolve().as_posix(), "milvus unavailable")]


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

        def delete_by_source_except_ids(self, source: str, vector_ids: list[str]) -> int:
            self.cleaned_batches.append((source, vector_ids))
            return 4

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.upserted_sources: list[str] = []

        def upsert_source(self, source: str, documents) -> None:
            self.upserted_sources.append(source)

        def mark_source_stale(self, source: str, reason: str) -> None:
            raise AssertionError(f"unexpected stale mark: {source} {reason}")

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
        def add_documents(self, documents):
            return [f"vec-{index}" for index, _ in enumerate(documents, 1)]

        def delete_by_source_except_ids(self, source: str, vector_ids: list[str]) -> int:
            raise RuntimeError("cleanup unavailable")

    class RecordingLexicalIndex:
        def __init__(self) -> None:
            self.upserted_sources: list[str] = []
            self.stale_sources: list[tuple[str, str]] = []

        def upsert_source(self, source: str, documents) -> None:
            self.upserted_sources.append(source)

        def mark_source_stale(self, source: str, reason: str) -> None:
            self.stale_sources.append((source, reason))

    fake_lexical = RecordingLexicalIndex()

    monkeypatch.setattr(vector_index_module.config, "index_allowed_roots", str(tmp_path))
    monkeypatch.setattr(
        vector_index_module,
        "vector_store_manager",
        FailingCleanupVectorStoreManager(),
    )
    monkeypatch.setattr(vector_index_module, "lexical_index_service", fake_lexical)

    with pytest.raises(RuntimeError, match="索引文件失败"):
        service.index_single_file(str(runbook))

    normalized_path = runbook.resolve().as_posix()
    assert fake_lexical.upserted_sources == []
    assert fake_lexical.stale_sources == [(normalized_path, "cleanup unavailable")]


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
    assert fake_lexical.stale_sources == [(normalized_path, "delete unavailable")]
