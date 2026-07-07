"""HTML loader for exported Wiki or Runbook pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from app.services.document_loaders.base import (
    DocumentCleaningReport,
    LoadedDocument,
    base_metadata,
    filter_loaded_documents,
    normalize_text,
)


class HtmlDocumentLoader:
    """Load local HTML files while removing navigation and script noise."""

    loader_type = "html"
    supported_extensions = {"html", "htm"}

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(
            [
                "script",
                "style",
                "nav",
                "footer",
                "header",
                "noscript",
                "svg",
                "aside",
                "menu",
                "form",
                "button",
                "input",
                "select",
                "textarea",
            ]
        ):
            tag.decompose()

        raw_units = _extract_heading_sections(path, soup)
        if not raw_units:
            text = normalize_text(soup.get_text("\n"))
            raw_units = [
                LoadedDocument(
                    content=text,
                    metadata=base_metadata(path, doc_type="html") | {"heading_path": ""},
                )
            ]
        return filter_loaded_documents(path, loader_type=self.loader_type, raw_units=raw_units)


def _extract_heading_sections(path: Path, soup: BeautifulSoup) -> list[LoadedDocument]:
    metadata_base = base_metadata(path, doc_type="html")
    sections: list[LoadedDocument] = []
    current_headings: dict[int, str] = {}
    current_title = ""
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_parts
        content = normalize_text("\n".join(current_parts))
        if not content:
            current_parts = []
            return
        heading_path = _heading_path(current_headings)
        sections.append(
            LoadedDocument(
                content=f"{current_title}\n{content}".strip() if current_title else content,
                metadata=metadata_base
                | {
                    "heading_path": heading_path,
                    "html_title": current_title,
                    **_heading_metadata(current_headings),
                },
            )
        )
        current_parts = []

    body = soup.body or soup
    for element in body.find_all(["h1", "h2", "h3", "p", "li", "pre", "code", "td", "th"]):
        name = str(element.name or "").lower()
        text = normalize_text(element.get_text(" "))
        if not text:
            continue
        if name in {"h1", "h2", "h3"}:
            flush()
            level = int(name[1])
            current_headings = {
                existing_level: value
                for existing_level, value in current_headings.items()
                if existing_level < level
            }
            current_headings[level] = text
            current_title = text
            continue
        current_parts.append(text)
    flush()
    return sections


def _heading_path(headings: dict[int, str]) -> str:
    return " > ".join(value for _, value in sorted(headings.items()))


def _heading_metadata(headings: dict[int, str]) -> dict[str, Any]:
    return {f"h{level}": value for level, value in headings.items() if level in {1, 2, 3}}
