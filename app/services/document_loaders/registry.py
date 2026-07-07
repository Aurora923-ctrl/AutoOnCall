"""Registry for source-specific document loaders."""

from __future__ import annotations

from pathlib import Path

from app.services.document_loaders.base import DocumentLoader
from app.services.document_loaders.html_loader import HtmlDocumentLoader
from app.services.document_loaders.pdf_loader import PdfDocumentLoader
from app.services.document_loaders.plain_text import PlainTextLoader
from app.services.document_loaders.table_loader import TableDocumentLoader


class DocumentLoaderRegistry:
    """Resolve loaders by file extension."""

    def __init__(self, loaders: list[DocumentLoader] | None = None) -> None:
        self._loaders = loaders or [
            PlainTextLoader(),
            PdfDocumentLoader(),
            HtmlDocumentLoader(),
            TableDocumentLoader(),
        ]

    @property
    def supported_extensions(self) -> set[str]:
        values: set[str] = set()
        for loader in self._loaders:
            values.update(loader.supported_extensions)
        return values

    def get_loader(self, path: str | Path) -> DocumentLoader:
        extension = Path(path).suffix.lower().removeprefix(".")
        for loader in self._loaders:
            if extension in loader.supported_extensions:
                return loader
        supported = ", ".join(sorted(self.supported_extensions))
        raise ValueError(f"不支持的文件格式: {extension or '无扩展名'}，支持: {supported}")


document_loader_registry = DocumentLoaderRegistry()
