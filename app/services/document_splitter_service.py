"""文档分割服务模块 - 基于 LangChain 的智能文档分割"""

import hashlib
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from loguru import logger

from app.config import config


class DocumentSplitterService:
    """文档分割服务 - 使用 LangChain 的分割器"""

    def __init__(self):
        """初始化文档分割服务"""
        self.chunk_size = config.chunk_max_size
        self.chunk_overlap = config.chunk_overlap

        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                # 不再按三级标题分割，避免过度碎片化
            ],
            strip_headers=False,  # 保留标题在内容中
        )

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size * 2,  # 加倍chunk_size，减少分片数
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

        logger.info(
            f"文档分割服务初始化完成, chunk_size={self.chunk_size}, "
            f"secondary_chunk_size={self.chunk_size * 2}, "
            f"overlap={self.chunk_overlap}"
        )

    def split_markdown(self, content: str, file_path: str = "") -> list[Document]:
        """
        分割 Markdown 文档 (两阶段分割 + 合并小片段)

        Args:
            content: Markdown 内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"Markdown 文档内容为空: {file_path}")
            return []

        try:
            md_docs = self.markdown_splitter.split_text(content)

            docs_after_split = self.text_splitter.split_documents(md_docs)

            final_docs = self._merge_small_chunks(docs_after_split, min_size=300)

            for index, doc in enumerate(final_docs, 1):
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = Path(file_path).suffix
                doc.metadata["_file_name"] = Path(file_path).name
                doc.metadata["_doc_id"] = file_path
                doc.metadata["_chunk_id"] = _build_chunk_id(file_path, index)
                doc.metadata.update(build_version_metadata(file_path, content, doc.page_content))

            logger.info(f"Markdown 分割完成: {file_path} -> {len(final_docs)} 个分片")
            return final_docs

        except Exception as e:
            logger.error(f"Markdown 分割失败: {file_path}, 错误: {e}")
            raise

    def split_text(self, content: str, file_path: str = "") -> list[Document]:
        """
        分割普通文本文档

        Args:
            content: 文本内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"文本文档内容为空: {file_path}")
            return []

        try:
            docs = list(
                self.text_splitter.create_documents(
                    texts=[content],
                    metadatas=[
                        {
                            "_source": file_path,
                            "_extension": Path(file_path).suffix,
                            "_file_name": Path(file_path).name,
                            "_doc_id": file_path,
                        }
                    ],
                )
            )
            for index, doc in enumerate(docs, 1):
                doc.metadata["_chunk_id"] = _build_chunk_id(file_path, index)
                doc.metadata.update(build_version_metadata(file_path, content, doc.page_content))

            logger.info(f"文本分割完成: {file_path} -> {len(docs)} 个分片")
            return docs

        except Exception as e:
            logger.error(f"文本分割失败: {file_path}, 错误: {e}")
            raise

    def split_document(self, content: str, file_path: str = "") -> list[Document]:
        """
        智能分割文档 (根据文件类型选择分割器)

        Args:
            content: 文档内容
            file_path: 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        if Path(file_path).suffix in {".md", ".markdown"}:
            return self.split_markdown(content, file_path)
        else:
            return self.split_text(content, file_path)

    def _merge_small_chunks(self, documents: list[Document], min_size: int = 300) -> list[Document]:
        """
        合并太小的分片

        Args:
            documents: 文档列表
            min_size: 最小分片大小 (字符数)

        Returns:
            List[Document]: 合并后的文档列表
        """
        if not documents:
            return []

        merged_docs = []
        current_doc = None

        for doc in documents:
            doc_size = len(doc.page_content)

            if current_doc is None:
                current_doc = doc
            elif (
                doc_size < min_size
                and len(current_doc.page_content) < self.chunk_size * 2
                and _same_markdown_heading(current_doc, doc)
            ):
                current_doc.page_content += "\n\n" + doc.page_content
            else:
                merged_docs.append(current_doc)
                current_doc = doc

        if current_doc is not None:
            merged_docs.append(current_doc)

        return merged_docs


document_splitter_service = DocumentSplitterService()


def _build_chunk_id(file_path: str, index: int) -> str:
    """Build a stable human-readable chunk id for one source document."""
    file_name = Path(file_path).name or "document"
    return f"{file_name}#{index:04d}"


def _same_markdown_heading(left: Document, right: Document) -> bool:
    """Return True when two chunks share the same Markdown heading metadata."""
    heading_keys = ("h1", "h2")
    return all(left.metadata.get(key) == right.metadata.get(key) for key in heading_keys)


def build_version_metadata(
    file_path: str, document_content: str, chunk_content: str
) -> dict[str, str]:
    """Build deterministic document version metadata for retrieval filtering and auditing."""
    document_hash = hashlib.sha256(document_content.encode("utf-8")).hexdigest()
    chunk_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
    return {
        "_document_version": document_hash[:12],
        "_document_hash": document_hash,
        "_chunk_hash": chunk_hash,
        "_version_key": f"{Path(file_path).name}:{document_hash[:12]}",
    }
