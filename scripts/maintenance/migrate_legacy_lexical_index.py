"""Migrate the local lexical index to stable source identities.

The command is dry-run by default. ``--apply`` writes a backup and atomically
replaces the index file without changing chunk content or search terms.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import config  # noqa: E402
from app.services.document_splitter_service import canonical_source_id  # noqa: E402


def migrate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Normalize chunk metadata and stale-source keys."""
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("lexical index chunks must be a list")
    migrated_chunks: list[dict[str, Any]] = []
    changed_chunks = 0
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            raise ValueError("lexical index chunk must be an object")
        chunk = dict(raw_chunk)
        source_path = str(chunk.get("source_path") or "")
        if not source_path:
            raise ValueError("lexical index chunk has no source_path")
        metadata = dict(chunk.get("metadata") or {})
        expected_source_id = canonical_source_id(source_path)
        before = dict(metadata)
        metadata.setdefault("_source", source_path)
        metadata["_source_id"] = expected_source_id
        chunk["metadata"] = metadata
        migrated_chunks.append(chunk)
        if before != metadata:
            changed_chunks += 1

    stale_sources = payload.get("stale_sources") or {}
    if not isinstance(stale_sources, dict):
        raise ValueError("lexical index stale_sources must be an object")
    migrated_stale = {
        canonical_source_id(str(source)): str(reason) for source, reason in stale_sources.items()
    }
    migrated = {
        **payload,
        "version": 1,
        "chunks": migrated_chunks,
        "stale_sources": migrated_stale,
    }
    return migrated, {
        "chunks": len(migrated_chunks),
        "changed_chunks": changed_chunks,
        "stale_sources": len(migrated_stale),
        "changed_stale_sources": sum(
            1 for source in stale_sources if canonical_source_id(str(source)) != str(source)
        ),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace one JSON file atomically in its existing directory."""
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=config.rag_lexical_index_path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    index_path = Path(args.index)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    migrated, summary = migrate_payload(payload)
    summary["mode"] = "apply" if args.apply else "dry-run"
    if args.apply:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(args.backup or f"logs/lexical-index-backup-{timestamp}.json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        atomic_write_json(index_path, migrated)
        summary["backup"] = str(backup)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
