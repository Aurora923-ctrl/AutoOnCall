"""Tests for shared AIOps tool fallback policy."""

import pytest

from app.config import config
from app.tools.fallback import run_adapter_or_mock


async def _adapter_success() -> dict:
    return {"status": "success", "source": "adapter"}


async def _adapter_failure() -> dict:
    raise RuntimeError("adapter down")


def _mock_payload() -> dict:
    return {"status": "success", "source": "mock"}


@pytest.mark.asyncio
async def test_run_adapter_or_mock_returns_mock_when_unconfigured_and_mock_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)

    result = await run_adapter_or_mock(
        configured=False,
        adapter_call=_adapter_success,
        mock_call=_mock_payload,
        source="demo",
        required_config="DEMO_URL",
        failure_summary_prefix="Demo query failed",
        not_configured_summary_prefix="Demo query unavailable",
    )

    assert result["source"] == "mock"


@pytest.mark.asyncio
async def test_run_adapter_or_mock_returns_not_configured_when_mock_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", False)

    result = await run_adapter_or_mock(
        configured=False,
        adapter_call=_adapter_success,
        mock_call=_mock_payload,
        source="demo",
        required_config="DEMO_URL",
        failure_summary_prefix="Demo query failed",
        not_configured_summary_prefix="Demo query unavailable",
        unavailable_defaults={"items": []},
    )

    assert result["status"] == "failed"
    assert result["source"] == "demo"
    assert result["error_type"] == "not_configured"
    assert result["items"] == []


@pytest.mark.asyncio
async def test_run_adapter_or_mock_respects_failure_fallback_flag(monkeypatch) -> None:
    monkeypatch.setattr(config, "aiops_mock_fallback_enabled", True)

    strict_result = await run_adapter_or_mock(
        configured=True,
        adapter_call=_adapter_failure,
        mock_call=_mock_payload,
        source="demo",
        required_config="DEMO_URL",
        failure_summary_prefix="Demo query failed",
        not_configured_summary_prefix="Demo query unavailable",
    )
    fallback_result = await run_adapter_or_mock(
        configured=True,
        adapter_call=_adapter_failure,
        mock_call=_mock_payload,
        source="demo",
        required_config="DEMO_URL",
        failure_summary_prefix="Demo query failed",
        not_configured_summary_prefix="Demo query unavailable",
        allow_failure_fallback=True,
    )

    assert strict_result["status"] == "failed"
    assert strict_result["source"] == "demo"
    assert fallback_result["source"] == "mock"
    assert fallback_result["source_quality"] == "mixed_with_fallback"
    assert fallback_result["fallback_reason"] == "adapter_failure"
    assert fallback_result["partial_errors"][0]["source"] == "demo"
    assert fallback_result["partial_errors"][0]["error_type"] == "adapter_error"
    assert "adapter down" not in fallback_result["partial_errors"][0]["message"]
