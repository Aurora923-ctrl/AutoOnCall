"""Regenerate the official knowledge snapshot hash manifest."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs" / "knowledge-base"
MANIFEST_PATH = ROOT / "docs" / "knowledge-base-official-sources.md"


def update_official_snapshot_manifest(
    *,
    manifest_path: Path = MANIFEST_PATH,
    docs_dir: Path = DOCS_DIR,
) -> None:
    """Replace only the manifest hash table, in stable filename order."""
    rows: list[bytes] = []
    for path in sorted(docs_dir.glob("official_*.md"), key=lambda item: item.name):
        digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        rows.append(f"| `{path.name}` | `{digest}` |".encode())
    if not rows:
        raise ValueError(f"no official snapshots found in {docs_dir}")

    manifest = manifest_path.read_bytes()
    pattern = re.compile(
        rb"(?ms)(^## Current cleaned hashes\r?\n\r?\n).*?"
        rb"(?=^## Distribution boundary\r?\n)"
    )
    match = pattern.search(manifest)
    if match is None:
        raise ValueError(f"hash section not found in {manifest_path}")
    newline = b"\r\n" if b"\r\n" in match.group(1) else b"\n"
    table = newline.join(
        [
            b"| Local file | SHA-256 |",
            b"| --- | --- |",
            *rows,
        ]
    )
    updated, count = pattern.subn(
        lambda section: section.group(1) + table + newline + newline,
        manifest,
        count=1,
    )
    if count != 1:
        raise ValueError(f"hash section not found in {manifest_path}")
    if updated != manifest:
        manifest_path.write_bytes(updated)


if __name__ == "__main__":
    update_official_snapshot_manifest()
