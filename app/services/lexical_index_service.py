"""Local lexical index for independent RAG keyword recall."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.documents import Document
from loguru import logger

from app.config import config

DEFAULT_LEXICAL_INDEX_PATH = Path(config.rag_lexical_index_path)


class LexicalIndexService:
    """Persist and search a tiny BM25-like local index for RAG chunks."""

    def __init__(self, index_path: str | Path = DEFAULT_LEXICAL_INDEX_PATH) -> None:
        self.index_path = Path(index_path)
        self._lock = threading.Lock()

    def upsert_source(self, source_path: str, documents: list[Document]) -> None:
        """Replace all chunks for one source in the local lexical index."""
        with self._lock:
            payload = self._load_index()
            chunks = [
                self._document_to_record(document, source_path=source_path, rank=rank)
                for rank, document in enumerate(documents, 1)
            ]
            payload["chunks"] = [
                chunk for chunk in payload["chunks"] if chunk.get("source_path") != source_path
            ]
            payload["chunks"].extend(chunks)
            self._save_index(payload)
        logger.info(f"本地词法索引更新完成: source={source_path}, chunks={len(chunks)}")

    def delete_source(self, source_path: str) -> int:
        """Delete all lexical chunks for one source."""
        with self._lock:
            payload = self._load_index()
            before = len(payload["chunks"])
            payload["chunks"] = [
                chunk for chunk in payload["chunks"] if chunk.get("source_path") != source_path
            ]
            deleted = before - len(payload["chunks"])
            if deleted:
                self._save_index(payload)
            return deleted

    def search(
        self,
        query: str,
        *,
        top_k: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[tuple[Document, float]]:
        """Search the local lexical index and return Document/score pairs."""
        payload = self._load_index()
        query_terms = extract_lexical_terms(query)
        if not query_terms:
            return []

        chunks = [
            chunk
            for chunk in payload["chunks"]
            if _metadata_matches_filter(chunk.get("metadata", {}), metadata_filter)
        ]
        scored: list[tuple[Document, float]] = []
        doc_count = max(len(chunks), 1)
        avg_len = _average([len(chunk.get("terms", [])) for chunk in chunks])
        document_frequency = _document_frequency(chunks)
        for chunk in chunks:
            score = bm25_like_score(
                query_terms,
                chunk_terms=list(chunk.get("terms") or []),
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

    def _document_to_record(
        self, document: Document, *, source_path: str, rank: int
    ) -> dict[str, Any]:
        metadata = dict(document.metadata or {})
        content = str(document.page_content or "")
        metadata.setdefault("_source", source_path)
        metadata.setdefault("_chunk_id", f"{Path(source_path).name}#{rank:04d}")
        metadata.setdefault("_file_name", Path(source_path).name)
        return {
            "source_path": source_path,
            "chunk_id": metadata.get("_chunk_id"),
            "content": content,
            "metadata": metadata,
            "terms": sorted(extract_lexical_terms(_searchable_text(content, metadata))),
        }

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 1, "chunks": []}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"读取本地词法索引失败，将使用空索引: {exc}")
            return {"version": 1, "chunks": []}
        chunks = payload.get("chunks", []) if isinstance(payload, dict) else []
        return {"version": 1, "chunks": chunks if isinstance(chunks, list) else []}

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


def extract_lexical_terms(text: str) -> set[str]:
    """Extract ASCII tokens plus Chinese bi/tri-grams for lexical retrieval."""
    lowered = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", lowered))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.update("".join(cjk_chars[index : index + 2]) for index in range(len(cjk_chars) - 1))
    terms.update("".join(cjk_chars[index : index + 3]) for index in range(len(cjk_chars) - 2))
    return {term for term in terms if term.strip()}


def bm25_like_score(
    query_terms: set[str],
    *,
    chunk_terms: list[str],
    document_frequency: dict[str, int],
    document_count: int,
    avg_len: float,
) -> float:
    """Return a compact BM25-like score without external dependencies."""
    if not query_terms or not chunk_terms:
        return 0.0
    k1 = 1.2
    b = 0.75
    length = len(chunk_terms)
    frequencies = {term: chunk_terms.count(term) for term in set(chunk_terms)}
    score = 0.0
    for term in query_terms:
        tf = frequencies.get(term, 0)
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


def _metadata_matches_filter(
    metadata: dict[str, Any],
    metadata_filter: dict[str, Any] | None,
) -> bool:
    if not metadata_filter:
        return True
    for key, expected in metadata_filter.items():
        actual = metadata.get(key)
        if isinstance(expected, list):
            if str(actual) not in {str(item) for item in expected}:
                return False
        elif str(actual) != str(expected):
            return False
    return True


def _average(values: list[int]) -> float:
    return sum(values) / max(len(values), 1)


lexical_index_service = LexicalIndexService()
