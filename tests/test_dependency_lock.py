"""Tests for reproducible dependency installation boundaries."""

from scripts.maintenance.verify_dependency_lock import main


def test_supported_installation_paths_consume_uv_lock() -> None:
    assert main() == 0
