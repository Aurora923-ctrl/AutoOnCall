"""Keep repository-level tests isolated from the local interview runtime store."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="autooncall-pytest-"))
os.environ["AIOPS_STORAGE_BACKEND"] = "sqlite"
os.environ["AIOPS_SQLITE_PATH"] = str(TEST_RUNTIME_DIR / "aiops_state.db")
os.environ["AIOPS_FEEDBACK_PATH"] = str(TEST_RUNTIME_DIR / "aiops_feedback.jsonl")
os.environ["AIOPS_TOOL_OUTPUT_ARTIFACT_DIR"] = str(TEST_RUNTIME_DIR / "aiops_tool_artifacts")
os.environ["RAG_LEXICAL_INDEX_PATH"] = str(TEST_RUNTIME_DIR / "rag_lexical_index.json")
os.environ["KNOWLEDGE_INDEXING_REPORT_PATH"] = str(
    TEST_RUNTIME_DIR / "knowledge_indexing_reports.jsonl"
)
