"""HTTP adapters for CMDB and deployment-history demo services."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config


class CMDBAdapter:
    """Read service ownership and dependency metadata from a CMDB-like API."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url if url is not None else config.cmdb_api_url
        self.token = token if token is not None else config.cmdb_api_bearer_token
        self.timeout_seconds = timeout_seconds or config.cmdb_api_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def query_service(self, service_name: str) -> dict[str, Any]:
        base_url = require_config(self.url, "CMDB_API_URL")
        payload = await _get_json(
            f"{base_url}/services/{service_name}.json",
            token=self.token,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
        )
        service = payload.get("service", payload)
        dependencies = service.get("dependencies", [])
        return adapter_success(
            source="cmdb",
            summary=f"CMDB 返回 {service_name} 的 owner、namespace 和 {len(dependencies)} 个依赖",
            signals={
                "dependency_count": len(dependencies),
                "has_owner": bool(service.get("owner")),
            },
            raw=payload,
            service_name=service_name,
            service=service,
            owner=service.get("owner", ""),
            namespace=service.get("namespace", ""),
            dependencies=dependencies,
        )


class DeployHistoryAdapter:
    """Read recent deployment/change metadata for one service."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url if url is not None else config.deploy_history_api_url
        self.token = token if token is not None else config.deploy_history_api_bearer_token
        self.timeout_seconds = timeout_seconds or config.deploy_history_api_timeout_seconds
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def query_deployments(self, service_name: str, limit: int = 5) -> dict[str, Any]:
        base_url = require_config(self.url, "DEPLOY_HISTORY_API_URL")
        payload = await _get_json(
            f"{base_url}/deployments/{service_name}.json",
            token=self.token,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
        )
        deployments = payload.get(
            "deployments",
            payload.get("recent_deployments", payload.get("items", [])),
        )
        if not isinstance(deployments, list):
            deployments = []
        deployments = deployments[: min(max(int(limit), 1), 20)]
        recent_change = deployments[0] if deployments else {}
        return adapter_success(
            source="deploy_history",
            summary=f"发布历史返回 {service_name} 最近 {len(deployments)} 条变更",
            signals={
                "deployment_count": len(deployments),
                "latest_status": recent_change.get("status", ""),
            },
            raw=payload,
            service_name=service_name,
            deployments=deployments,
            recent_change=recent_change,
        )


async def _get_json(
    url: str,
    *,
    token: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        headers=bearer_headers(token),
        transport=transport,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {"items": payload}
