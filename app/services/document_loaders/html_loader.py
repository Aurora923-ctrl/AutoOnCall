"""HTML loader for exported Wiki or Runbook pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.services.document_loaders.base import (
    DocumentCleaningReport,
    LoadedDocument,
    base_metadata,
    filter_loaded_documents,
    normalize_text,
)

MAX_HTML_SECTIONS = 10_000
HTML_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
HTML_CONTENT_TAGS = HTML_HEADING_TAGS | {"p", "li", "pre", "code", "td", "th"}
HIDDEN_STYLE_MARKERS = ("display:none", "display: none", "visibility:hidden", "visibility: hidden")


class HtmlDocumentLoader:
    """Load local HTML files while removing navigation and script noise."""

    loader_type = "html"
    supported_extensions = {"html", "htm"}

    def load(self, path: Path) -> tuple[list[LoadedDocument], DocumentCleaningReport]:
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
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
        for tag in soup.find_all(_is_hidden_tag):
            tag.decompose()

        raw_units, truncated = _extract_heading_sections(path, soup)
        if not raw_units:
            text = normalize_text(soup.get_text("\n"))
            raw_units = [
                LoadedDocument(
                    content=text,
                    metadata=base_metadata(path, doc_type="html") | {"heading_path": ""},
                )
            ]
        documents, report = filter_loaded_documents(
            path,
            loader_type=self.loader_type,
            raw_units=raw_units,
        )
        if truncated:
            report.add_warning(f"HTML ignored sections after {MAX_HTML_SECTIONS}")
        return documents, report


def _extract_heading_sections(
    path: Path,
    soup: BeautifulSoup,
) -> tuple[list[LoadedDocument], bool]:
    metadata_base = base_metadata(path, doc_type="html")
    sections: list[LoadedDocument] = []
    truncated = False
    current_headings: dict[int, str] = {}
    current_title = ""
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_parts, truncated
        content = normalize_text("\n".join(current_parts))
        if not content:
            current_parts = []
            return
        if len(sections) >= MAX_HTML_SECTIONS:
            truncated = True
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
    for element in body.find_all(HTML_CONTENT_TAGS):
        if _has_content_tag_ancestor(element, body):
            continue
        name = str(element.name or "").lower()
        text = normalize_text(element.get_text(" "))
        if not text:
            continue
        if name in HTML_HEADING_TAGS:
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
    return sections, truncated


def _is_hidden_tag(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if str(tag.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = str(tag.get("style") or "").replace(" ", "").lower()
    return any(marker.replace(" ", "") in style for marker in HIDDEN_STYLE_MARKERS)


def _has_content_tag_ancestor(element: Tag, body: Tag) -> bool:
    """Avoid indexing nested semantic blocks twice, such as li > p or pre > code."""
    parent = element.parent
    while isinstance(parent, Tag) and parent is not body:
        if str(parent.name or "").lower() in HTML_CONTENT_TAGS:
            return True
        parent = parent.parent
    return False


def _heading_path(headings: dict[int, str]) -> str:
    return " > ".join(value for _, value in sorted(headings.items()))


def _heading_metadata(headings: dict[int, str]) -> dict[str, Any]:
    return {f"h{level}": value for level, value in headings.items() if 1 <= level <= 6}
