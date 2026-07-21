from __future__ import annotations

import pytest

from app.services import service_topology


def test_service_topology_invalid_yaml_fails_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_path = tmp_path / "service_topology.yaml"
    topology_path.write_text("services: [", encoding="utf-8")
    monkeypatch.setattr(service_topology.config, "service_topology_path", str(topology_path))
    service_topology.load_service_topology.cache_clear()

    with pytest.raises(service_topology.ServiceTopologyError, match="could not be loaded"):
        service_topology.load_service_topology()

    service_topology.load_service_topology.cache_clear()


def test_service_topology_rejects_ambiguous_primary_instance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_path = tmp_path / "service_topology.yaml"
    topology_path.write_text(
        "services:\n"
        "  order-service:\n"
        "    redis:\n"
        "      - redis-a\n"
        "      - redis-b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_topology.config, "service_topology_path", str(topology_path))
    service_topology.load_service_topology.cache_clear()

    with pytest.raises(service_topology.ServiceTopologyError, match="Multiple redis instances"):
        service_topology.get_primary_dependency_instance("order-service", "redis")

    service_topology.load_service_topology.cache_clear()


def test_service_topology_rejects_non_string_instance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_path = tmp_path / "service_topology.yaml"
    topology_path.write_text(
        "services:\n"
        "  order-service:\n"
        "    redis:\n"
        "      - 123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_topology.config, "service_topology_path", str(topology_path))
    service_topology.load_service_topology.cache_clear()

    with pytest.raises(service_topology.ServiceTopologyError, match="must be non-empty strings"):
        service_topology.get_primary_dependency_instance("order-service", "redis")

    service_topology.load_service_topology.cache_clear()
