"""Plain text and Markdown document loaders."""

from __future__ import annotations

from pathlib import Path

from app.services.document_loaders.base import (
    DocumentCleaningReport,
    LoadedDocument,
    base_metadata,
    filter_loaded_documents,
)


class PlainTextLoader:
    """Load UTF-8 text-like files as one logical document."""

    loader_type = "plain_text"
    supported_extensions = {"txt", "md", "markdown"}

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        doc_type = "markdown" if path.suffix.lower() in {".md", ".markdown"} else "text"
        raw = [
            LoadedDocument(
                content=path.read_text(encoding="utf-8-sig"),
                metadata=base_metadata(path, doc_type=doc_type),
            )
        ]
        return filter_loaded_documents(path, loader_type=self.loader_type, raw_units=raw)
