"""Tests for multi-source RAG document loaders."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from app.services.document_loaders.html_loader import HtmlDocumentLoader
from app.services.document_loaders.pdf_loader import PdfDocumentLoader
from app.services.document_loaders.registry import document_loader_registry
from app.services.document_loaders.table_loader import TableDocumentLoader
from app.services.rag_answer_policy import ensure_citation_block
from app.services.rag_read_models import compact_retrieval_chunk
from scripts.data.generate_demo_rag_assets import main as generate_demo_rag_assets


def test_loader_registry_supports_enterprise_knowledge_formats() -> None:
    assert {
        "txt",
        "md",
        "markdown",
        "pdf",
        "html",
        "htm",
        "csv",
        "xlsx",
    }.issubset(document_loader_registry.supported_extensions)


def test_html_loader_removes_navigation_and_preserves_heading_path(tmp_path: Path) -> None:
    html = tmp_path / "wiki.html"
    html.write_text(
        """
        <html>
          <body>
            <nav>首页 导航 联系我们</nav>
            <h1>Redis 故障处理</h1>
            <h2>maxclients 耗尽</h2>
            <p>order-service 出现 Redis connection timeout，需要检查 connected_clients。</p>
            <aside>related docs noise should be removed</aside>
            <form><button>submit noisy search form</button></form>
            <footer>版权信息</footer>
            <script>alert("noise")</script>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    docs, report = HtmlDocumentLoader().load(html)

    assert report.raw_units >= 1
    assert report.indexed_units == 1
    assert docs[0].metadata["doc_type"] == "html"
    assert docs[0].metadata["heading_path"] == "Redis 故障处理 > maxclients 耗尽"
    assert "connected_clients" in docs[0].content
    assert "首页 导航" not in docs[0].content
    assert "related docs noise" not in docs[0].content
    assert "submit noisy search" not in docs[0].content
    assert "alert" not in docs[0].content


def test_csv_loader_turns_rows_into_citable_knowledge_units(tmp_path: Path) -> None:
    csv_file = tmp_path / "tickets.csv"
    csv_file.write_text(
        "ticket_id,service_name,root_cause,resolution\n"
        "INC-REDIS-001,order-service,Redis maxclients exhausted,raise maxclients after approval\n",
        encoding="utf-8",
    )

    docs, report = TableDocumentLoader().load(csv_file)

    assert report.indexed_units == 1
    assert docs[0].metadata["doc_type"] == "table"
    assert docs[0].metadata["sheet_name"] == "csv"
    assert docs[0].metadata["row_number"] == 2
    assert docs[0].metadata["primary_key"] == "ticket_id=INC-REDIS-001"
    assert "Redis maxclients exhausted" in docs[0].content


def test_csv_loader_drops_empty_rows_and_reports_truncation(tmp_path: Path) -> None:
    csv_file = tmp_path / "tickets.csv"
    long_resolution = "修复步骤" * 300
    csv_file.write_text(
        "ticket_id,service_name,resolution,extra1,extra2,extra3,extra4,extra5,extra6,extra7,"
        "extra8,extra9,extra10,extra11,extra12,extra13,extra14,extra15,extra16,extra17,"
        "extra18,extra19,extra20,extra21,extra22,extra23,extra24,extra25\n"
        f"INC-REDIS-001,order-service,{long_resolution},1,2,3,4,5,6,7,8,9,10,11,12,"
        "13,14,15,16,17,18,19,20,21,22,23,24,25\n"
        ",,,,,,,,,,,,,,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )

    docs, report = TableDocumentLoader().load(csv_file)

    assert len(docs) == 1
    assert report.raw_units == 2
    assert report.indexed_units == 1
    assert report.empty_units == 1
    assert any("truncated from" in warning for warning in report.warnings)
    assert any("ignored columns after" in warning for warning in report.warnings)
    assert "[truncated]" in docs[0].content


def test_xlsx_loader_preserves_sheet_and_row_locator(tmp_path: Path) -> None:
    xlsx_file = tmp_path / "service_catalog.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "services"
    sheet.append(["service_name", "owner", "dependency"])
    sheet.append(["payment-service", "payments-oncall", "mysql-payments"])
    workbook.save(xlsx_file)

    docs, report = TableDocumentLoader().load(xlsx_file)

    assert report.indexed_units == 1
    assert docs[0].metadata["sheet_name"] == "services"
    assert docs[0].metadata["row_number"] == 2
    assert docs[0].metadata["primary_key"] == "service_name=payment-service"
    assert "mysql-payments" in docs[0].content


def test_pdf_loader_keeps_page_number_and_reports_empty_pages(monkeypatch, tmp_path: Path) -> None:
    pdf_file = tmp_path / "postmortem.pdf"
    pdf_file.write_bytes(b"%PDF fake for monkeypatched reader")

    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    def fake_reader(_path: str) -> SimpleNamespace:
        return SimpleNamespace(
            pages=[
                FakePage("Redis maxclients 故障复盘，connected_clients 达到 9940。"),
                FakePage(""),
            ]
        )

    monkeypatch.setattr("app.services.document_loaders.pdf_loader.PdfReader", fake_reader)

    docs, report = PdfDocumentLoader().load(pdf_file)

    assert report.raw_units == 2
    assert report.indexed_units == 1
    assert report.empty_units == 1
    assert docs[0].metadata["doc_type"] == "pdf"
    assert docs[0].metadata["page_number"] == 1
    assert "connected_clients" in docs[0].content


def test_citation_block_includes_pdf_and_table_locators() -> None:
    answer = "基于知识库，Redis 连接数接近上限。"
    rendered = ensure_citation_block(
        answer,
        [
            {
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "redis_postmortem.pdf#0001",
                "page_number": 3,
                "score": 0.12,
            },
            {
                "source_file": "tickets.xlsx",
                "chunk_id": "tickets.xlsx#0002",
                "sheet_name": "incidents",
                "row_number": 12,
                "primary_key": "ticket_id=INC-REDIS-001",
                "score": 0.2,
            },
        ],
    )

    assert "page_number: 3" in rendered
    assert "sheet_name: incidents" in rendered
    assert "row_number: 12" in rendered
    assert "primary_key: ticket_id=INC-REDIS-001" in rendered


def test_compact_retrieval_chunk_exposes_source_specific_locators() -> None:
    compact = compact_retrieval_chunk(
        {
            "source_file": "tickets.xlsx",
            "chunk_id": "tickets.xlsx#0002",
            "metadata": {
                "doc_type": "table",
                "sheet_name": "incidents",
                "row_number": 12,
                "primary_key": "ticket_id=INC-REDIS-001",
            },
        }
    )

    assert compact["doc_type"] == "table"
    assert compact["sheet_name"] == "incidents"
    assert compact["row_number"] == 12
    assert compact["primary_key"] == "ticket_id=INC-REDIS-001"


def test_generated_demo_rag_assets_are_loader_readable() -> None:
    generate_demo_rag_assets()

    expected = {
        "redis_postmortem.pdf": "pdf",
        "mysql_slow_query_postmortem.pdf": "pdf",
        "redis_capacity_wiki.html": "html",
        "payment_wiki.html": "html",
        "tickets.csv": "table",
        "tickets.xlsx": "table",
    }
    for file_name, loader_type in expected.items():
        path = Path("aiops-docs") / file_name
        loader = document_loader_registry.get_loader(path)
        docs, report = loader.load(path)

        assert loader.loader_type == loader_type
        assert docs
        assert report.indexed_units >= 1
