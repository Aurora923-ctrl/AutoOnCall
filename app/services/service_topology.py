"""Lightweight service dependency topology for AIOps planning."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import config


class ServiceTopologyError(RuntimeError):
    """Raised when configured topology cannot safely select a dependency instance."""


@lru_cache(maxsize=1)
def load_service_topology() -> dict[str, Any]:
    """Load optional service topology from YAML."""
    path = Path(config.service_topology_path)
    if not path.exists():
        raise ServiceTopologyError(f"Service topology file does not exist: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ServiceTopologyError("Service topology could not be loaded") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise ServiceTopologyError("Service topology must contain a services mapping")
    return payload


def get_service_dependencies(service_name: str) -> dict[str, Any]:
    """Return dependency metadata for one service."""
    topology = load_service_topology()
    services = topology.get("services") if isinstance(topology, dict) else {}
    if not isinstance(services, dict):
        return {}
    payload = services.get(service_name) or {}
    return payload if isinstance(payload, dict) else {}


def service_has_dependency(service_name: str, dependency_type: str) -> bool:
    """Return True when service topology declares a dependency type."""
    dependencies = get_service_dependencies(service_name)
    values = dependencies.get(dependency_type)
    return isinstance(values, list) and bool(values)


def get_dependency_instances(service_name: str, dependency_type: str) -> list[str]:
    """Return dependency instance names declared for one service."""
    dependencies = get_service_dependencies(service_name)
    values = dependencies.get(dependency_type)
    if not isinstance(values, list):
        return []
    instances: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ServiceTopologyError(
                f"Invalid {dependency_type} instance for {service_name}; "
                "topology values must be non-empty strings"
            )
        instance = value.strip()
        if instance not in instances:
            instances.append(instance)
    return instances


def get_primary_dependency_instance(service_name: str, dependency_type: str) -> str:
    """Return the sole declared instance, rejecting ambiguous topology."""
    instances = get_dependency_instances(service_name, dependency_type)
    if len(instances) > 1:
        raise ServiceTopologyError(
            f"Multiple {dependency_type} instances are configured for {service_name}; "
            "the plan must select one explicitly"
        )
    return instances[0] if instances else ""
