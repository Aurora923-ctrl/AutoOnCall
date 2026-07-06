"""Boundary tests for local monitor MCP helpers."""

import pytest

pytest.importorskip("fastmcp")

from mcp_servers.monitor_server import parse_interval_minutes


def test_monitor_interval_parser_rejects_zero_interval() -> None:
    assert parse_interval_minutes("5m") == 5
    assert parse_interval_minutes("1h") == 60

    with pytest.raises(ValueError, match="greater than 0"):
        parse_interval_minutes("0m")
