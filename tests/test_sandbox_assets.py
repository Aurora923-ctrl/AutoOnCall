from __future__ import annotations

import importlib.util
from pathlib import Path

from app.config import LOCAL_FULL_STACK_ENV

ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_compose_declares_real_dependency_services() -> None:
    compose = (ROOT / "deploy" / "compose" / "interview-stack.yml").read_text(encoding="utf-8")

    for service_name in [
        "autooncall-full-redis",
        "autooncall-full-mysql",
        "autooncall-full-prometheus",
        "autooncall-full-metrics-exporter",
        "autooncall-full-loki",
        "autooncall-full-loki-log-emitter",
    ]:
        assert service_name in compose

    assert 'profiles: ["advanced"]' not in compose
    assert "full-stack-compose.yml" not in compose
    assert "autooncall-full-mysql-business-seed" not in compose
    assert "autooncall-full-redis-business-seed" not in compose
    assert "autooncall-full-alertmanager" not in compose
    assert "autooncall-full-jaeger" not in compose
    assert "autooncall-full-tempo" not in compose
    assert "autooncall-full-otel" not in compose
    assert "autooncall-full-grafana" not in compose
    assert "autooncall-full-redpanda" not in compose
    assert "autooncall-full-cmdb-mock" not in compose
    assert "autooncall-full-ticketing-mock" not in compose
    assert "autooncall-full-deploy-history-mock" not in compose
    assert "autooncall-full-kubernetes-mock" not in compose
    assert "127.0.0.1:16379:6379" in compose
    assert "127.0.0.1:13306:3306" in compose
    assert "127.0.0.1:19090:9090" in compose
    assert "D:/AppDataStorage/DockerData/autooncall-full" in compose


def test_interview_stack_defaults_to_core_aiops_without_milvus() -> None:
    interview = (ROOT / "deploy" / "compose" / "interview-stack.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    sandbox_doc = (ROOT / "deploy" / "sandbox.md").read_text(encoding="utf-8")

    assert "full-stack-compose.yml" not in interview
    assert "vector-database.yml" not in interview
    assert "autooncall-full-mysql-business-seed" not in interview
    assert "autooncall-full-redis-business-seed" not in interview
    assert "docker compose -f deploy/compose/interview-stack.yml up -d --remove-orphans" in makefile
    assert "scripts/sandbox/seed_live_incident_evidence.py" in makefile
    assert "make up && make upload" in makefile
    assert "Redis, MySQL, metrics-exporter, Prometheus, Loki, and loki-log-emitter" in sandbox_doc
    assert "starts only the six core services" in sandbox_doc
    assert (
        "K8s CrashLoop/OOMKilled is intentionally treated as an offline golden regression case"
        in sandbox_doc
    )


def test_sandbox_env_disables_mock_and_wires_adapters() -> None:
    env_text = (ROOT / "deploy" / "sandbox.env").read_text(encoding="utf-8")

    assert "AIOPS_MOCK_FALLBACK_ENABLED=false" in env_text
    assert "PROMETHEUS_BASE_URL=http://127.0.0.1:19090" in env_text
    assert "KUBERNETES_API_SERVER=http://127.0.0.1:18085" not in env_text
    assert "KUBERNETES_NAMESPACE=default" in env_text
    assert "ALERTMANAGER_BASE_URL=http://127.0.0.1:19093" not in env_text
    assert "JAEGER_BASE_URL=http://127.0.0.1:16686" not in env_text
    assert "REDPANDA_ADMIN_URL=http://127.0.0.1:19644" not in env_text
    assert "CMDB_API_URL=" not in env_text
    assert "DEPLOY_HISTORY_API_URL=" not in env_text
    assert "TICKET_API_URL=" not in env_text
    assert "REDIS_INSTANCES=" in env_text
    assert "MYSQL_INSTANCES=" in env_text
    assert 'autooncall_p95_latency_ms{service="{service_name}"}' in env_text


def test_pycharm_launcher_uses_adapter_endpoint_shapes() -> None:
    script = (ROOT / "scripts" / "dev" / "pycharm_one_click_start.py").read_text(encoding="utf-8")

    assert "LOCAL_ADVANCED_STACK_ENV" not in script
    assert LOCAL_FULL_STACK_ENV["CMDB_API_URL"] == ""
    assert LOCAL_FULL_STACK_ENV["DEPLOY_HISTORY_API_URL"] == ""
    assert LOCAL_FULL_STACK_ENV["TICKET_API_URL"] == ""
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_API_SERVER"] == ""
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_NAMESPACE"] == "default"
    assert LOCAL_FULL_STACK_ENV["KUBERNETES_VERIFY_SSL"] == "false"
    assert "GRAFANA_URL" not in LOCAL_FULL_STACK_ENV
    assert '"CMDB_API_URL": "http://127.0.0.1:18081/index.json"' not in script
    assert '"DEPLOY_HISTORY_API_URL": "http://127.0.0.1:18084/index.json"' not in script
    assert '"TICKET_API_URL": "http://127.0.0.1:18083/index.json"' not in script


def test_sandbox_demo_script_targets_full_stack_compose() -> None:
    script = (ROOT / "scripts" / "sandbox" / "simulate_mysql_redis_aiops.py").read_text(
        encoding="utf-8"
    )

    assert 'SANDBOX_COMPOSE = ROOT / "deploy" / "compose" / "interview-stack.yml"' in script
    assert 'SANDBOX_REDIS_CONTAINER = "autooncall-full-redis"' in script
    assert 'SANDBOX_MYSQL_CONTAINER = "autooncall-full-mysql"' in script
    assert 'SANDBOX_PROMETHEUS_CONTAINER = "autooncall-full-prometheus"' in script
    assert 'SANDBOX_LOKI_CONTAINER = "autooncall-full-loki"' in script
    assert 'SANDBOX_LOKI_EMITTER_CONTAINER = "autooncall-full-loki-log-emitter"' in script
    assert "Default interview stack contains only MySQL, Redis, metrics-exporter" in script
    assert "def prometheus_ready_url()" in script
    assert 'PROMETHEUS_READY_URL = "http://127.0.0.1:19090/-/ready"' not in script
    assert "autooncall-sandbox-" not in script


def test_sandbox_metrics_exporter_renders_prometheus_series() -> None:
    exporter_path = ROOT / "deploy" / "adapters" / "metrics_exporter.py"
    spec = importlib.util.spec_from_file_location("sandbox_metrics_exporter", exporter_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    metrics = module.render_metrics()

    assert 'autooncall_http_qps{service="order-service"}' in metrics
    assert 'autooncall_p95_latency_ms{service="payment-service"}' in metrics
    assert 'http_requests_total{service="checkout-service",status="500"}' in metrics
    assert 'autooncall_business_success_rate{service="order-service"}' in metrics
    assert 'autooncall_business_backlog{service="inventory-service"}' in metrics


def test_prometheus_rules_use_exported_metric_names_and_are_loaded() -> None:
    rules = (ROOT / "deploy" / "adapters" / "alert-rules.yml").read_text(encoding="utf-8")
    prometheus = (ROOT / "deploy" / "adapters" / "prometheus.yml").read_text(encoding="utf-8")

    assert 'autooncall_http_5xx_rate{service="order-service"} > 0.05' in rules
    assert 'autooncall_p95_latency_ms{service="order-service"} > 1000' in rules
    assert "autooncall_http_error_rate" not in rules
    assert "autooncall_http_p95_latency_ms" not in rules
    assert "rule_files:" in prometheus
    assert "/etc/prometheus/alert-rules.yml" in prometheus


def test_sandbox_business_fixtures_are_interview_ready() -> None:
    mysql_seed = ROOT / "deploy" / "adapters" / "mysql-init" / "001_init.sql"
    loki_emitter = ROOT / "deploy" / "adapters" / "loki_log_emitter.py"

    seed_text = mysql_seed.read_text(encoding="utf-8")
    loki_text = loki_emitter.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS aiops_service_catalog" in seed_text
    assert "CREATE TABLE IF NOT EXISTS aiops_deploy_history" in seed_text
    assert "CREATE TABLE IF NOT EXISTS aiops_history_tickets" in seed_text
    assert "CREATE TABLE IF NOT EXISTS aiops_incident_evidence" in seed_text
    assert "live-mysql-seed" in seed_text
    assert "critical_user_journey" in seed_text
    assert "POST /api/orders" in seed_text
    assert "customer_impact" in seed_text
    assert "prevention" in seed_text
    assert "business_reason" in seed_text
    assert "related_config" in seed_text
    assert "checkout_success_rate=0.918" in loki_text


def test_mysql_redis_golden_seed_uses_live_sources() -> None:
    mysql_seed = (ROOT / "deploy" / "adapters" / "mysql-init" / "001_init.sql").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "deploy" / "compose" / "interview-stack.yml").read_text(encoding="utf-8")
    seed_script = (ROOT / "scripts" / "sandbox" / "seed_live_incident_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "live-redis-seed" in seed_script
    assert "autooncall:incident:order-service:redis-maxclients" in seed_script
    assert "$(SANDBOX_SEED_ARGS)" in (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "$(SANDBOX_VERIFY_ARGS)" in (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "live-mysql-seed" in mysql_seed
    assert "live-redis-seed" not in compose
    assert "demo-seed" not in mysql_seed
    assert "autooncall-seed" not in mysql_seed
    assert "incident_demo_cases" not in mysql_seed


def test_sandbox_business_http_mocks_are_removed() -> None:
    assert not (ROOT / "deploy" / "adapters" / "mock-cmdb").exists()
    assert not (ROOT / "deploy" / "adapters" / "mock-ticketing").exists()
    assert not (ROOT / "deploy" / "adapters" / "mock-deploy-history").exists()


def test_k8s_golden_case_is_explicitly_offline_fixture() -> None:
    cases = (ROOT / "eval" / "cases.yaml").read_text(encoding="utf-8")
    eval_script = (ROOT / "scripts" / "eval" / "eval_cases.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "evidence_mode: offline_fixture" in cases
    assert "not live container-backed evidence" in cases
    assert "golden_evidence_mode" in eval_script
    assert "K8s CrashLoop/OOMKilled 只作为 offline golden regression case" in readme
