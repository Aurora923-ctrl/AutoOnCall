"""Adapters for service catalog and deployment-history evidence."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import adapter_success, bearer_headers, require_config
from app.integrations.mysql_business_data import MySQLBusinessDataAdapter


class CMDBAdapter:
    """Read service ownership and dependency metadata from a CMDB-like API."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        mysql_adapter: MySQLBusinessDataAdapter | None = None,
    ):
        self.url = url if url is not None else config.cmdb_api_url
        self.token = token if token is not None else config.cmdb_api_bearer_token
        self.timeout_seconds = timeout_seconds or config.cmdb_api_timeout_seconds
        self.transport = transport
        self.mysql_adapter = mysql_adapter or MySQLBusinessDataAdapter()

    @property
    def configured(self) -> bool:
        return bool(self.url or self.mysql_adapter.configured)

    async def query_service(self, service_name: str) -> dict[str, Any]:
        if self.url:
            base_url = require_config(self.url, "CMDB_API_URL")
            payload = await _get_json(
                f"{base_url}/services/{service_name}.json",
                token=self.token,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
            )
        else:
            payload = await self.mysql_adapter.query_service_catalog(service_name)
        service = payload.get("service", payload)
        dependencies = service.get("dependencies", [])
        business_context = service.get("business_context", {})
        critical_endpoints = service.get("critical_endpoints", [])
        slo = service.get("slo", {})
        tier = service.get("tier", "")
        return adapter_success(
            source="cmdb",
            summary=(
                f"CMDB 返回 {service_name} 的 owner、namespace、tier={tier or 'unknown'} "
                f"和 {len(dependencies)} 个依赖"
            ),
            signals={
                "dependency_count": len(dependencies),
                "has_owner": bool(service.get("owner")),
                "has_business_context": bool(business_context),
                "critical_endpoint_count": len(critical_endpoints)
                if isinstance(critical_endpoints, list)
                else 0,
            },
            raw=payload,
            service_name=service_name,
            service=service,
            owner=service.get("owner", ""),
            namespace=service.get("namespace", ""),
            tier=tier,
            dependencies=dependencies,
            business_context=business_context if isinstance(business_context, dict) else {},
            critical_endpoints=critical_endpoints if isinstance(critical_endpoints, list) else [],
            slo=slo if isinstance(slo, dict) else {},
            runbooks=service.get("runbooks", []),
        )


class DeployHistoryAdapter:
    """Read recent deployment/change metadata for one service."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        mysql_adapter: MySQLBusinessDataAdapter | None = None,
    ):
        self.url = url if url is not None else config.deploy_history_api_url
        self.token = token if token is not None else config.deploy_history_api_bearer_token
        self.timeout_seconds = timeout_seconds or config.deploy_history_api_timeout_seconds
        self.transport = transport
        self.mysql_adapter = mysql_adapter or MySQLBusinessDataAdapter()

    @property
    def configured(self) -> bool:
        return bool(self.url or self.mysql_adapter.configured)

    async def query_deployments(self, service_name: str, limit: int = 5) -> dict[str, Any]:
        if self.url:
            base_url = require_config(self.url, "DEPLOY_HISTORY_API_URL")
            payload = await _get_json(
                f"{base_url}/deployments/{service_name}.json",
                token=self.token,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
            )
        else:
            payload = await self.mysql_adapter.query_deploy_history(service_name)
        deployments = payload.get(
            "deployments",
            payload.get("recent_deployments", payload.get("items", [])),
        )
        if not isinstance(deployments, list):
            deployments = []
        deployments = deployments[: min(max(int(limit), 1), 20)]
        recent_change = deployments[0] if deployments else {}
        high_risk_changes = [
            item
            for item in deployments
            if str(item.get("risk") or "").lower() in {"high", "critical"}
        ]
        return adapter_success(
            source="deploy_history",
            summary=(
                f"发布历史返回 {service_name} 最近 {len(deployments)} 条变更，"
                f"latest_status={recent_change.get('status', '') or 'unknown'}"
            ),
            signals={
                "deployment_count": len(deployments),
                "latest_status": recent_change.get("status", ""),
                "high_risk_change_count": len(high_risk_changes),
            },
            raw=payload,
            service_name=service_name,
            current_version=payload.get("current_version", ""),
            deployments=deployments,
            recent_change=recent_change,
            high_risk_changes=high_risk_changes,
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
