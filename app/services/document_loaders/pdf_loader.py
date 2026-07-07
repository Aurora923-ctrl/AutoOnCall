"""PDF loader for text-based incident reports and SOP documents."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from app.services.document_loaders.base import (
    DocumentCleaningReport,
    LoadedDocument,
    base_metadata,
    filter_loaded_documents,
    normalize_text,
)


class PdfDocumentLoader:
    """Extract page-level text from non-scanned PDF documents."""

    loader_type = "pdf"
    supported_extensions = {"pdf"}

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        reader = PdfReader(str(path))
        raw_units: list[LoadedDocument] = []
        metadata_base = base_metadata(path, doc_type="pdf")
        for page_index, page in enumerate(reader.pages, 1):
            text = normalize_text(page.extract_text() or "")
            raw_units.append(
                LoadedDocument(
                    content=text,
                    metadata=metadata_base | {"page_number": page_index},
                )
            )
        documents, report = filter_loaded_documents(
            path,
            loader_type=self.loader_type,
            raw_units=raw_units,
        )
        if not documents and raw_units:
            report.warnings.append("PDF 未提取到有效文本；扫描件需要 OCR 后再入库")
        return documents, report
