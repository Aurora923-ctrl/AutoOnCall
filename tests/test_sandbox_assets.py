from __future__ import annotations

import importlib.util
from pathlib import Path

from app.config import LOCAL_FULL_STACK_ENV

ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_compose_declares_real_dependency_services() -> None:
    compose = (ROOT / "deploy" / "full-stack-compose.yml").read_text(encoding="utf-8")

    for service_name in [
        "autooncall-full-redis",
        "autooncall-full-mysql",
        "autooncall-full-prometheus",
        "autooncall-full-metrics-exporter",
        "autooncall-full-loki",
        "autooncall-full-kubernetes-mock",
        "autooncall-full-redpanda",
    ]:
        assert service_name in compose

    assert "127.0.0.1:16379:6379" in compose
    assert "127.0.0.1:13306:3306" in compose
    assert "127.0.0.1:19090:9090" in compose
    assert "127.0.0.1:18085:8080" in compose
    assert "D:/AppDataStorage/DockerData/autooncall-full" in compose


def test_sandbox_env_disables_mock_and_wires_adapters() -> None:
    env_text = (ROOT / "deploy" / "sandbox.env").read_text(encoding="utf-8")

    assert "AIOPS_MOCK_FALLBACK_ENABLED=false" in env_text
    assert "ALERTMANAGER_BASE_URL=http://127.0.0.1:19093" in env_text
    assert "PROMETHEUS_BASE_URL=http://127.0.0.1:19090" in env_text
    assert "TICKET_API_URL=http://127.0.0.1:18083/tickets.json" in env_text
    assert "KUBERNETES_API_SERVER=http://127.0.0.1:18085" in env_text
    assert "KUBERNETES_NAMESPACE=default" in env_text
    assert "JAEGER_BASE_URL=http://127.0.0.1:16686" in env_text
    assert "REDPANDA_ADMIN_URL=http://127.0.0.1:19644" in env_text
    assert "REDIS_INSTANCES=" in env_text
    assert "MYSQL_INSTANCES=" in env_text
    assert 'autooncall_p95_latency_ms{service="{service_name}"}' in env_text


def test_pycharm_launcher_uses_adapter_endpoint_shapes() -> None:
    script = (ROOT / "scripts" / "pycharm_one_click_start.py").read_text(encoding="utf-8")

    assert "from app.config import LOCAL_DEMO_API_URL, LOCAL_FULL_STACK_ENV" in script
    assert LOCAL_FULL_STACK_ENV["CMDB_API_URL"] == "http://127.0.0.1:18081"
    assert LOCAL_FULL_STACK_ENV["DEPLOY_HISTORY_API_URL"] == "http://127.0.0.1:18084"
    assert LOCAL_FULL_STACK_ENV["TICKET_API_URL"] == "http://127.0.0.1:18083/tickets.json"
    assert LOCAL_FULL_STACK_ENV["GRAFANA_URL"] == "http://127.0.0.1:13000"
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_API_SERVER"] == "http://127.0.0.1:18085"
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_NAMESPACE"] == "default"
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_VERIFY_SSL"] == "false"
    assert '"CMDB_API_URL": "http://127.0.0.1:18081/index.json"' not in script
    assert '"DEPLOY_HISTORY_API_URL": "http://127.0.0.1:18084/index.json"' not in script
    assert '"TICKET_API_URL": "http://127.0.0.1:18083/index.json"' not in script
    assert "LOCAL_FULL_STACK_ENV['GRAFANA_URL']" in script
    assert "LOCAL_FULL_STACK_ENV['JAEGER_BASE_URL']" in script
    assert "LOCAL_FULL_STACK_ENV['REDPANDA_ADMIN_URL']" in script


def test_sandbox_demo_script_targets_full_stack_compose() -> None:
    script = (ROOT / "scripts" / "simulate_mysql_redis_aiops.py").read_text(encoding="utf-8")

    assert 'SANDBOX_COMPOSE = ROOT / "deploy" / "full-stack-compose.yml"' in script
    assert 'SANDBOX_REDIS_CONTAINER = "autooncall-full-redis"' in script
    assert 'SANDBOX_MYSQL_CONTAINER = "autooncall-full-mysql"' in script
    assert 'SANDBOX_PROMETHEUS_CONTAINER = "autooncall-full-prometheus"' in script
    assert "def prometheus_ready_url()" in script
    assert 'PROMETHEUS_READY_URL = "http://127.0.0.1:19090/-/ready"' not in script
    assert "autooncall-sandbox-" not in script


def test_sandbox_kubernetes_mock_exposes_crashloop_fixture() -> None:
    resources = ROOT / "deploy" / "full-stack" / "mock-kubernetes" / "resources.json"
    server = ROOT / "deploy" / "full-stack" / "mock-kubernetes" / "server.py"

    assert resources.exists()
    assert server.exists()

    text = resources.read_text(encoding="utf-8")
    assert "inventory-service-6d7c9b-crash1" in text
    assert "CrashLoopBackOff" in text
    assert "BackOff" in text


def test_sandbox_metrics_exporter_renders_prometheus_series() -> None:
    exporter_path = ROOT / "deploy" / "sandbox" / "metrics_exporter.py"
    spec = importlib.util.spec_from_file_location("sandbox_metrics_exporter", exporter_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    metrics = module.render_metrics()

    assert 'autooncall_http_qps{service="order-service"}' in metrics
    assert 'autooncall_p95_latency_ms{service="payment-service"}' in metrics
    assert 'http_requests_total{service="checkout-service",status="500"}' in metrics
