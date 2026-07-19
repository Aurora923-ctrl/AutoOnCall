"""Sensitive logging boundaries for local MCP fixtures."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastmcp")

from mcp_servers import cls_server, monitor_server


@pytest.mark.parametrize(
    ("module", "logger_name"),
    [
        (monitor_server, "Monitor_MCP_Server"),
        (cls_server, "CLS_MCP_Server"),
    ],
)
def test_mcp_tool_call_log_does_not_emit_raw_arguments(
    caplog: pytest.LogCaptureFixture,
    module: object,
    logger_name: str,
) -> None:
    @module.log_tool_call
    def sample_tool(**kwargs: object) -> dict[str, object]:
        return {"status": "success", "received": len(kwargs)}

    with caplog.at_level(logging.INFO, logger=logger_name):
        result = sample_tool(
            query="password=super-secret",
            authorization="Bearer hidden-token",
        )

    log_text = caplog.text
    assert result["status"] == "success"
    assert "super-secret" not in log_text
    assert "hidden-token" not in log_text
    assert "password=" not in log_text
    assert "tool_args_sha256=" in log_text


@pytest.mark.parametrize(
    ("module", "logger_name"),
    [
        (monitor_server, "Monitor_MCP_Server"),
        (cls_server, "CLS_MCP_Server"),
    ],
)
def test_mcp_tool_call_log_does_not_emit_raw_exception_text(
    caplog: pytest.LogCaptureFixture,
    module: object,
    logger_name: str,
) -> None:
    @module.log_tool_call
    def failing_tool(**_: object) -> None:
        raise RuntimeError("token=exception-secret")

    with caplog.at_level(logging.ERROR, logger=logger_name):
        with pytest.raises(RuntimeError, match="exception-secret"):
            failing_tool()

    assert "exception-secret" not in caplog.text
    assert "error_sha256=" in caplog.text
