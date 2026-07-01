"""Lightweight service dependency topology for AIOps planning."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import config


@lru_cache(maxsize=1)
def load_service_topology() -> dict[str, Any]:
    """Load optional service topology from YAML."""
    path = Path(config.service_topology_path)
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    return [str(value) for value in values if value]


def get_primary_dependency_instance(service_name: str, dependency_type: str) -> str:
    """Return the first declared dependency instance for one service."""
    instances = get_dependency_instances(service_name, dependency_type)
    return instances[0] if instances else ""
