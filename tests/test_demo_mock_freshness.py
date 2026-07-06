"""Tests for interview/demo mock data freshness."""

from __future__ import annotations

from datetime import datetime

from app.tools.logs_tool import QueryLogsTool
from app.tools.mock_ops_tool import QueryK8sStatusTool


def test_mock_logs_use_recent_timestamps() -> None:
    now = datetime.now()
    logs = QueryLogsTool._mock_logs("order-service")

    for item in logs:
        timestamp = datetime.strptime(item["timestamp"], "%Y-%m-%d %H:%M:%S")
        assert 0 <= (now - timestamp).total_seconds() < 10 * 60


def test_mock_k8s_deployment_timestamp_is_recent() -> None:
    now = datetime.now()
    payload = QueryK8sStatusTool._mock_status("inventory-service", "10m")

    updated_at = datetime.strptime(payload["deployment"]["updated_at"], "%Y-%m-%d %H:%M:%S")
    assert 0 <= (now - updated_at).total_seconds() < 60 * 60
    assert payload["deployment"]["image"].endswith(now.strftime("%Y.%m.%d"))
