"""Local lexical index for independent RAG keyword recall."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from langchain_core.documents import Document
from loguru import logger

from app.config import config
from app.services.document_splitter_service import canonical_source_id

DEFAULT_LEXICAL_INDEX_PATH = Path(config.rag_lexical_index_path)


class LexicalIndexService:
    """Persist and search a tiny BM25-like local index for RAG chunks."""

    def __init__(self, index_path: str | Path = DEFAULT_LEXICAL_INDEX_PATH) -> None:
        self.index_path = Path(index_path)
        self._lock = threading.Lock()
        self.lock_path = self.index_path.with_name(f".{self.index_path.name}.lock")

    def upsert_source(
        self,
        source_path: str,
        documents: list[Document],
        *,
        clear_stale: bool = True,
    ) -> None:
        """Replace all chunks for one source in the local lexical index."""
        source_id = canonical_source_id(source_path)
        with self._locked_payload():
            payload = self._load_index(strict=True)
            self._replace_source_in_payload(
                payload,
                source_path=source_path,
                source_id=source_id,
                documents=documents,
                clear_stale=clear_stale,
            )
            self._save_index(payload)
        logger.info(f"本地词法索引更新完成: source={source_path}, chunks={len(documents)}")

    def replace_source_and_clear_stale(
        self,
        source_path: str,
        documents: list[Document],
    ) -> None:
        """Atomically replace one source and make the new lexical version retrievable."""
        self.upsert_source(source_path, documents, clear_stale=True)

    def delete_source(self, source_path: str) -> int:
        """Delete all lexical chunks for one source."""
        source_id = canonical_source_id(source_path)
        with self._locked_payload():
            payload = self._load_index(strict=True)
            before = len(payload["chunks"])
            payload["chunks"] = [
                chunk
                for chunk in payload["chunks"]
                if canonical_source_id(str(chunk.get("source_path") or "")) != source_id
            ]
            deleted = before - len(payload["chunks"])
            stale_removed = source_id in payload["stale_sources"]
            payload["stale_sources"].pop(source_id, None)
            if deleted or stale_removed:
                self._save_index(payload)
            return deleted

    def clear(self) -> None:
        """Atomically reset all lexical chunks and stale-source markers."""
        with self._locked_payload():
            self._save_index({"version": 1, "chunks": [], "stale_sources": {}})

    def replace_all_sources(self, documents_by_source: dict[str, list[Document]]) -> None:
        """Atomically replace the complete lexical index."""
        payload: dict[str, Any] = {"version": 1, "chunks": [], "stale_sources": {}}
        for source_path, documents in documents_by_source.items():
            source_id = canonical_source_id(source_path)
            self._replace_source_in_payload(
                payload,
                source_path=source_path,
                source_id=source_id,
                documents=documents,
                clear_stale=True,
            )
        with self._locked_payload():
            self._save_index(payload)

    def snapshot_index_state(self) -> dict[str, Any]:
        """Return a complete restorable index snapshot."""
        with self._locked_payload():
            return deepcopy(self._load_index(strict=True))

    def restore_index_state(self, snapshot: dict[str, Any]) -> None:
        """Restore a complete snapshot after a failed maintenance rebuild."""
        with self._locked_payload():
            self._save_index(deepcopy(snapshot))

    def snapshot_source_state(self, source_path: str) -> dict[str, Any]:
        """Return a restorable snapshot for one source before a cross-index update."""
        source_id = canonical_source_id(source_path)
        with self._locked_payload():
            payload = self._load_index(strict=True)
        return {
            "source_id": source_id,
            "chunks": deepcopy(
                [
                    chunk
                    for chunk in payload["chunks"]
                    if canonical_source_id(str(chunk.get("source_path") or "")) == source_id
                ]
            ),
            "stale_reason": payload["stale_sources"].get(source_id),
        }

    def restore_source_state(self, source_path: str, snapshot: dict[str, Any]) -> None:
        """Restore one source snapshot after a failed vector/lexical transaction."""
        source_id = canonical_source_id(source_path)
        snapshot_chunks = deepcopy(snapshot.get("chunks") or [])
        with self._locked_payload():
            payload = self._load_index(strict=True)
            payload["chunks"] = [
                chunk
                for chunk in payload["chunks"]
                if canonical_source_id(str(chunk.get("source_path") or "")) != source_id
            ]
            payload["chunks"].extend(snapshot_chunks)
            stale_reason = snapshot.get("stale_reason")
            if stale_reason:
                payload["stale_sources"][source_id] = str(stale_reason)
            else:
                payload["stale_sources"].pop(source_id, None)
            self._save_index(payload)

    def identity_records(self) -> list[dict[str, str]]:
        """Return lexical identity fields for rebuild and consistency verification."""
        with self._locked_payload():
            payload = self._load_index(strict=True)
        records: list[dict[str, str]] = []
        for chunk in payload["chunks"]:
            metadata = dict(chunk.get("metadata") or {})
            records.append(
                {
                    "source_file": str(metadata.get("_file_name") or ""),
                    "source_id": str(metadata.get("_source_id") or ""),
                    "chunk_id": str(metadata.get("_chunk_id") or chunk.get("chunk_id") or ""),
                    "document_hash": str(metadata.get("_document_hash") or ""),
                    "chunk_hash": str(metadata.get("_chunk_hash") or ""),
                }
            )
        return records

    def mark_source_stale(self, source_path: str, reason: str) -> None:
        """Mark a source as stale so retrieval will not trust old chunks for it."""
        source_id = canonical_source_id(source_path)
        with self._locked_payload():
            payload = self._load_index(strict=True)
            payload["stale_sources"][source_id] = str(reason or "indexing_failed")[:500]
            self._save_index(payload)
        logger.warning(f"本地词法索引标记为陈旧: source={source_path}, reason={reason}")

    def clear_source_stale(self, source_path: str) -> None:
        """Clear the stale marker for a source after a successful index update."""
        source_id = canonical_source_id(source_path)
        with self._locked_payload():
            payload = self._load_index(strict=True)
            if source_id not in payload["stale_sources"]:
                return
            payload["stale_sources"].pop(source_id, None)
            self._save_index(payload)

    def is_source_stale(self, source_path: str) -> bool:
        """Return True when a source should be excluded from retrieval."""
        with self._locked_payload():
            payload = self._load_index(strict=True)
        return canonical_source_id(source_path) in payload["stale_sources"]

    def search(
        self,
        query: str,
        *,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[tuple[Document, float]]:
        """Search the local lexical index and return Document/score pairs."""
        safe_query = str(query or "").strip()
        if not safe_query:
            raise ValueError("query 不能为空")
        if len(safe_query) > 8000:
            raise ValueError("query 长度不能超过 8000")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        with self._locked_payload():
            payload = self._load_index(strict=True)
        query_terms = extract_lexical_terms(safe_query)
        if not query_terms:
            return []

        stale_sources = set(payload["stale_sources"])
        chunks = [
            chunk
            for chunk in payload["chunks"]
            if canonical_source_id(str(chunk.get("source_path") or "")) not in stale_sources
            if _metadata_matches_filter(chunk.get("metadata", {}), metadata_filter)
        ]
        scored: list[tuple[Document, float]] = []
        doc_count = max(len(chunks), 1)
        avg_len = _average([len(chunk.get("terms", [])) for chunk in chunks])
        document_frequency = _document_frequency(chunks)
        for chunk in chunks:
            score = bm25_like_score(
                query_terms,
                term_frequencies=_record_term_frequencies(chunk),
                document_frequency=document_frequency,
                document_count=doc_count,
                avg_len=avg_len,
            )
            if score <= 0:
                continue
            metadata = dict(chunk.get("metadata") or {})
            metadata["_lexical_score"] = round(score, 4)
            scored.append(
                (
                    Document(page_content=str(chunk.get("content") or ""), metadata=metadata),
                    round(score, 4),
                )
            )

        scored.sort(key=lambda item: (-item[1], item[0].metadata.get("_chunk_id", "")))
        return scored[:top_k]

    def search_exact_entities(
        self,
        query: str,
        *,
        entities: set[str],
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[tuple[Document, float]]:
        """Return chunks containing exact incident, version, or primary-key entities."""
        normalized_entities = {str(item).strip().casefold() for item in entities if str(item).strip()}
        if not normalized_entities:
            return []
        with self._locked_payload():
            payload = self._load_index(strict=True)
        stale_sources = set(payload["stale_sources"])
        matches: list[tuple[Document, float]] = []
        for chunk in payload["chunks"]:
            source_path = str(chunk.get("source_path") or "")
            if canonical_source_id(source_path) in stale_sources:
                continue
            metadata = dict(chunk.get("metadata") or {})
            if not _metadata_matches_filter(metadata, metadata_filter):
                continue
            searchable = _searchable_text(str(chunk.get("content") or ""), metadata).casefold()
            matched = {
                entity
                for entity in normalized_entities
                if _contains_exact_entity(searchable, entity)
            }
            if not matched:
                continue
            metadata["_lexical_score"] = 100.0 + len(matched)
            metadata["_exact_entities"] = sorted(matched)
            metadata["_retrieval_source"] = "lexical"
            matches.append(
                (
                    Document(page_content=str(chunk.get("content") or ""), metadata=metadata),
                    100.0 + len(matched),
                )
            )
        matches.sort(
            key=lambda item: (
                -item[1],
                str(item[0].metadata.get("_file_name") or ""),
                str(item[0].metadata.get("_chunk_id") or ""),
            )
        )
        return matches[:top_k]

    def _document_to_record(
        self, document: Document, *, source_path: str, rank: int
    ) -> dict[str, Any]:
        metadata = dict(document.metadata or {})
        content = str(document.page_content or "")
        metadata.setdefault("_source", source_path)
        metadata.setdefault("_source_id", canonical_source_id(source_path))
        metadata.setdefault("_chunk_id", f"{Path(source_path).name}#{rank:04d}")
        metadata.setdefault("_file_name", Path(source_path).name)
        return {
            "source_path": source_path,
            "chunk_id": metadata.get("_chunk_id"),
            "content": content,
            "metadata": metadata,
            "terms": lexical_terms(_searchable_text(content, metadata)),
        }

    def _replace_source_in_payload(
        self,
        payload: dict[str, Any],
        *,
        source_path: str,
        source_id: str,
        documents: list[Document],
        clear_stale: bool,
    ) -> None:
        chunks = [
            self._document_to_record(document, source_path=source_path, rank=rank)
            for rank, document in enumerate(documents, 1)
        ]
        payload["chunks"] = [
            chunk
            for chunk in payload["chunks"]
            if canonical_source_id(str(chunk.get("source_path") or "")) != source_id
        ]
        payload["chunks"].extend(chunks)
        if clear_stale:
            payload["stale_sources"].pop(source_id, None)

    def _locked_payload(self) -> _CombinedLock:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        return _CombinedLock(self._lock, FileLock(str(self.lock_path)))

    def _load_index(self, *, strict: bool = False) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "chunks": [], "stale_sources": {}}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if strict:
                raise RuntimeError(f"读取本地词法索引失败: {exc}") from exc
            logger.warning(f"读取本地词法索引失败，将使用空索引: {exc}")
            return {"version": 1, "chunks": [], "stale_sources": {}}
        chunks = payload.get("chunks", []) if isinstance(payload, dict) else []
        stale_sources = payload.get("stale_sources", {}) if isinstance(payload, dict) else {}
        normalized_stale_sources = {}
        if isinstance(stale_sources, dict):
            normalized_stale_sources = {
                canonical_source_id(str(source_path)): str(reason)
                for source_path, reason in stale_sources.items()
            }
        return {
            "version": 1,
            "chunks": chunks if isinstance(chunks, list) else [],
            "stale_sources": normalized_stale_sources,
        }

    def _save_index(self, payload: dict[str, Any]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.index_path.with_name(f".{self.index_path.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp_path, self.index_path)
        finally:
            temp_path.unlink(missing_ok=True)


class _CombinedLock:
    def __init__(self, thread_lock: threading.Lock, file_lock: FileLock) -> None:
        self.thread_lock = thread_lock
        self.file_lock = file_lock

    def __enter__(self) -> _CombinedLock:
        self.thread_lock.acquire()
        try:
            self.file_lock.acquire()
        except Exception:
            self.thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.file_lock.release()
        finally:
            self.thread_lock.release()


def lexical_terms(text: str) -> list[str]:
    """Extract ASCII tokens plus Chinese bi/tri-grams for lexical retrieval."""
    lowered = str(text or "").lower()
    terms = re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.extend("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    terms.extend("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    return [term for term in terms if term.strip()]


def extract_lexical_terms(text: str) -> set[str]:
    """Extract unique terms for query matching and compatibility callers."""
    return set(lexical_terms(text))


def bm25_like_score(
    query_terms: set[str],
    *,
    term_frequencies: dict[str, int],
    document_frequency: dict[str, int],
    document_count: int,
    avg_len: float,
) -> float:
    """Return a compact BM25-like score without external dependencies."""
    if not query_terms or not term_frequencies:
        return 0.0
    k1 = 1.2
    b = 0.75
    length = sum(term_frequencies.values())
    score = 0.0
    for term in query_terms:
        tf = term_frequencies.get(term, 0)
        if tf <= 0:
            continue
        df = document_frequency.get(term, 0)
        idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
        denominator = tf + k1 * (1 - b + b * length / max(avg_len, 1.0))
        score += idf * ((tf * (k1 + 1)) / max(denominator, 1e-6))
    return score


def _searchable_text(content: str, metadata: dict[str, Any]) -> str:
    return " ".join(
        [
            str(metadata.get("_file_name") or ""),
            str(metadata.get("h1") or ""),
            str(metadata.get("h2") or ""),
            str(metadata.get("heading_path") or ""),
            content,
        ]
    )


def _document_frequency(chunks: list[dict[str, Any]]) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for chunk in chunks:
        for term in set(chunk.get("terms") or []):
            frequencies[term] = frequencies.get(term, 0) + 1
    return frequencies


def _record_term_frequencies(chunk: dict[str, Any]) -> dict[str, int]:
    terms = chunk.get("terms")
    frequencies: dict[str, int] = {}
    if isinstance(terms, list):
        for term in terms:
            normalized = str(term or "")
            if normalized:
                frequencies[normalized] = frequencies.get(normalized, 0) + 1
        return frequencies
    content = str(chunk.get("content") or "")
    metadata = dict(chunk.get("metadata") or {})
    for term in lexical_terms(_searchable_text(content, metadata)):
        frequencies[term] = frequencies.get(term, 0) + 1
    return frequencies


def _metadata_matches_filter(
    metadata: dict[str, Any],
    metadata_filter: dict[str, Any] | None,
) -> bool:
    if not metadata_filter:
        return True
    for key, expected in metadata_filter.items():
        actual = metadata.get(key)
        if isinstance(expected, list):
            if not any(_metadata_values_equal(actual, item) for item in expected):
                return False
        elif not _metadata_values_equal(actual, expected):
            return False
    return True


def _metadata_values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return float(actual) == float(expected)
    return type(actual) is type(expected) and actual == expected


def _contains_exact_entity(searchable: str, entity: str) -> bool:
    """Use token boundaries so rc1 does not match rc10 and IDs remain exact."""
    pattern = rf"(?<![a-z0-9_-]){re.escape(entity)}(?![a-z0-9_-])"
    return re.search(pattern, searchable, flags=re.IGNORECASE) is not None


def _average(values: list[int]) -> float:
    return sum(values) / max(len(values), 1)


lexical_index_service = LexicalIndexService()
