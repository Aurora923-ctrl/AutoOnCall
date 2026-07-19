"""Adapters for service catalog and deployment-history evidence."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import config
from app.integrations.base import (
    ExternalAdapterResponseError,
    adapter_success,
    bearer_headers,
    require_config,
    require_success_payload,
)
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
        if not isinstance(service, dict):
            raise ExternalAdapterResponseError("CMDB response service must be an object")
        dependencies = service.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ExternalAdapterResponseError("CMDB response dependencies must be an array")
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
                "critical_endpoint_count": (
                    len(critical_endpoints) if isinstance(critical_endpoints, list) else 0
                ),
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
            raise ExternalAdapterResponseError(
                "Deploy history response deployments must be an array"
            )
        deployments = deployments[: min(max(int(limit), 1), 20)]
        recent_change = deployments[0] if deployments else {}
        high_risk_changes = [
            item
            for item in deployments
            if str(item.get("risk") or "").lower() in {"high", "critical"}
        ]
        release_correlation = _release_correlation(service_name, recent_change)
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
                "feature_flag_change": bool(release_correlation),
            },
            raw=payload,
            service_name=service_name,
            current_version=payload.get("current_version", ""),
            deployments=deployments,
            recent_change=recent_change,
            high_risk_changes=high_risk_changes,
            release_correlation=release_correlation,
            fact=_release_fact(recent_change),
            inference=_release_inference(release_correlation),
            uncertainty=(
                "Release timing is supporting context. It raises the matching SQL-path "
                "hypothesis but cannot prove root cause without slow-query and pool evidence."
            ),
        )


def _release_correlation(service_name: str, recent_change: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            str(recent_change.get("summary") or ""),
            str(recent_change.get("business_reason") or ""),
            " ".join(str(item) for item in recent_change.get("related_config", []) if item),
        ]
    ).lower()
    if service_name != "payment-service":
        return {}
    if not any(
        token in text
        for token in [
            "payment_report_enabled=true",
            "reconciliation report",
            "date-range query",
            "report",
        ]
    ):
        return {}
    return {
        "change_id": recent_change.get("change_id", ""),
        "feature_flag": "PAYMENT_REPORT_ENABLED=true",
        "changed_path": "payment reconciliation report date-range query",
        "root_cause_role": "supporting_correlation",
    }


def _release_fact(recent_change: dict[str, Any]) -> str:
    if not recent_change:
        return "No recent deployment record was returned."
    return (
        f"Recent change {recent_change.get('change_id', 'unknown')} "
        f"({recent_change.get('status', 'unknown')}): "
        f"{recent_change.get('summary', 'no summary')}."
    )


def _release_inference(release_correlation: dict[str, Any]) -> str:
    if release_correlation:
        return (
            "The report Feature Flag introduced a matching date-range query path shortly "
            "before the incident, so the slow-report-SQL hypothesis should rank above a "
            "generic CPU explanation."
        )
    return "The release record provides timeline context but does not materially change ranking."


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
        payload = require_success_payload(
            response.json(),
            system_name="Business context API",
            allow_list=True,
        )
    return payload if isinstance(payload, dict) else {"items": payload}
