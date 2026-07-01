"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

import json
from typing import Any
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---- 本地演示启动配置 ---------------------------------------------------------
LOCAL_DEMO_HOST = "127.0.0.1"
LOCAL_DEMO_PORT = 9900
LOCAL_DEMO_API_URL = f"http://{LOCAL_DEMO_HOST}:{LOCAL_DEMO_PORT}"

LOCAL_FULL_STACK_ENV = {
    "HOST": LOCAL_DEMO_HOST,
    "PORT": str(LOCAL_DEMO_PORT),
    "MILVUS_HOST": LOCAL_DEMO_HOST,
    "MILVUS_PORT": "19530",
    "AIOPS_MOCK_FALLBACK_ENABLED": "true",
    "ALERTMANAGER_BASE_URL": "http://127.0.0.1:19093",
    "PROMETHEUS_BASE_URL": "http://127.0.0.1:19090",
    "LOG_GATEWAY_URL": "http://127.0.0.1:13100",
    "LOKI_BASE_URL": "http://127.0.0.1:13100",
    "GRAFANA_URL": "http://127.0.0.1:13000",
    "JAEGER_BASE_URL": "http://127.0.0.1:16686",
    "TEMPO_BASE_URL": "http://127.0.0.1:13200",
    "REDPANDA_ADMIN_URL": "http://127.0.0.1:19644",
    "KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:19092",
    "CMDB_API_URL": "http://127.0.0.1:18081",
    "DEPLOY_HISTORY_API_URL": "http://127.0.0.1:18084",
    "TICKET_API_URL": "http://127.0.0.1:18083/tickets.json",
    "KUBERNETES_API_SERVER": "http://127.0.0.1:18085",
    "KUBERNETES_NAMESPACE": "default",
    "KUBERNETES_VERIFY_SSL": "false",
    "REDIS_URL": "redis://127.0.0.1:16379/0",
    "REDIS_INSTANCES": '{"redis-cluster-prod":"redis://127.0.0.1:16379/0"}',
    "MYSQL_DSN": (
        "mysql+pymysql://autooncall:autooncall123@127.0.0.1:13306/autooncall" "?charset=utf8mb4"
    ),
    "MYSQL_INSTANCES": (
        '{"order-mysql":"mysql+pymysql://autooncall:autooncall123@127.0.0.1:13306/'
        'autooncall?charset=utf8mb4","payment-mysql":"mysql+pymysql://autooncall:'
        'autooncall123@127.0.0.1:13306/autooncall?charset=utf8mb4"}'
    ),
}


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用基础配置 ---------------------------------------------------------
    app_name: str = "AutoOnCall"
    app_version: str = "1.2.1"
    debug: bool = False
    host: str = LOCAL_DEMO_HOST
    port: int = 9900
    api_base_url: str = LOCAL_DEMO_API_URL

    # ---- 文件上传与运行产物路径配置 ------------------------------------------
    upload_dir: str = "uploads"
    upload_allowed_extensions: str = "txt,md,markdown"
    upload_max_file_size_mb: int = 10
    upload_read_chunk_size: int = 1024 * 1024
    eval_summary_path: str = "logs/eval_summary.json"
    adapter_verification_path: str = "logs/full_stack_adapter_verification.json"

    # ---- DashScope / LLM 配置 -------------------------------------------------
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # ---- Milvus 向量库配置 ----------------------------------------------------
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒
    milvus_recreate_on_dimension_mismatch: bool = False

    # ---- RAG 检索与索引配置 ---------------------------------------------------
    rag_top_k: int = 3
    rag_model: str = ""  # 为空时复用 DASHSCOPE_MODEL，避免两个模型配置互相漂移
    rag_max_l2_distance: float = 2.0
    rag_content_preview_chars: int = 240
    rag_hybrid_search_enabled: bool = True
    rag_hybrid_candidate_multiplier: int = 4
    rag_rerank_enabled: bool = True
    rag_min_lexical_trust_score: float = 0.20

    chunk_max_size: int = 800
    chunk_overlap: int = 100
    index_allowed_roots: str = "uploads,aiops-docs"
    rag_lexical_index_path: str = "data/rag_lexical_index.json"

    # ---- AIOps 运行态配置 -----------------------------------------------------
    # Trace、Approval、Report 默认写入 SQLite；设置 AIOPS_STORAGE_BACKEND=mysql
    # 时使用 MYSQL_DSN / MYSQL_HOST 等配置。
    aiops_storage_backend: str = "sqlite"
    aiops_sqlite_path: str = "data/aiops_state.db"
    aiops_mock_fallback_enabled: bool = False
    service_topology_path: str = "config/service_topology.yaml"

    # ---- 生产部署与日志配置 ---------------------------------------------------
    cors_allowed_origins: str = (
        f"http://{LOCAL_DEMO_HOST}:{LOCAL_DEMO_PORT},http://localhost:{LOCAL_DEMO_PORT}"
    )
    log_retention_days: int = 14

    # ---- 内部 API Token / RBAC 配置 ------------------------------------------
    # Optional internal API-token RBAC. Disabled by default for local demos/tests.
    # When enabled, missing token configuration fails closed.
    api_auth_enabled: bool = False
    api_auth_tokens: str = ""
    api_read_token: str = ""
    api_operator_token: str = ""
    api_approver_token: str = ""
    api_admin_token: str = ""

    # ---- Alertmanager 适配器配置 ---------------------------------------------
    alertmanager_base_url: str = ""
    alertmanager_bearer_token: str = ""
    alertmanager_timeout_seconds: float = 5.0

    # ---- Prometheus 适配器配置 ------------------------------------------------
    prometheus_base_url: str = ""
    prometheus_bearer_token: str = ""
    prometheus_timeout_seconds: float = 5.0
    prometheus_qps_query: str = 'sum(rate(http_requests_total{service="{service_name}"}[5m]))'
    prometheus_error_rate_query: str = (
        'sum(rate(http_requests_total{service="{service_name}",status=~"5.."}[5m])) / '
        'clamp_min(sum(rate(http_requests_total{service="{service_name}"}[5m])), 1)'
    )
    prometheus_p95_query: str = (
        "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket"
        '{service="{service_name}"}[5m])) by (le)) * 1000'
    )
    prometheus_cpu_query: str = (
        'avg(container_cpu_usage_seconds_total{pod=~"{service_name}.*"}) * 100'
    )
    prometheus_memory_query: str = (
        'avg(container_memory_working_set_bytes{pod=~"{service_name}.*"})'
    )

    # ---- 日志与调用链适配器配置 ----------------------------------------------
    log_gateway_url: str = ""
    log_gateway_bearer_token: str = ""
    log_gateway_timeout_seconds: float = 8.0
    loki_base_url: str = ""
    loki_bearer_token: str = ""
    loki_timeout_seconds: float = 8.0

    jaeger_base_url: str = ""
    jaeger_bearer_token: str = ""
    jaeger_timeout_seconds: float = 8.0
    tempo_base_url: str = ""
    tempo_bearer_token: str = ""
    tempo_timeout_seconds: float = 8.0

    # ---- 消息队列适配器配置 ---------------------------------------------------
    redpanda_admin_url: str = ""
    redpanda_bearer_token: str = ""
    redpanda_timeout_seconds: float = 8.0
    kafka_bootstrap_servers: str = ""

    # ---- CMDB / 发布历史适配器配置 -------------------------------------------
    cmdb_api_url: str = ""
    cmdb_api_bearer_token: str = ""
    cmdb_api_timeout_seconds: float = 8.0

    deploy_history_api_url: str = ""
    deploy_history_api_bearer_token: str = ""
    deploy_history_api_timeout_seconds: float = 8.0

    # ---- Kubernetes 适配器配置 ------------------------------------------------
    kubernetes_api_server: str = ""
    kubernetes_namespace: str = "default"
    kubernetes_bearer_token: str = ""
    kubernetes_verify_ssl: bool = True
    kubernetes_timeout_seconds: float = 8.0

    # ---- Redis 适配器配置 -----------------------------------------------------
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    redis_url: str = ""
    redis_instances: str = ""
    redis_timeout_seconds: float = 5.0
    redis_allow_admin_commands: bool = True

    # ---- MySQL 适配器与状态存储配置 ------------------------------------------
    mysql_dsn: str = ""
    mysql_url: str = ""
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""
    mysql_instances: str = ""
    mysql_timeout_seconds: float = 5.0
    aiops_store_raw_external_payload: bool = False

    # ---- 工单系统适配器配置 ---------------------------------------------------
    ticket_api_url: str = ""
    ticket_api_bearer_token: str = ""
    ticket_api_timeout_seconds: float = 8.0

    # ---- MCP 服务配置 ---------------------------------------------------------
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
        }

    @property
    def cors_origins(self) -> list[str]:
        """Return CORS origins parsed from a comma-separated environment value."""
        origins = [item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()]
        return origins or ["*"]

    @property
    def effective_rag_model(self) -> str:
        """Return the model used by RAG and AIOps LLM calls."""
        return self.rag_model or self.dashscope_model

    @property
    def log_file_retention(self) -> str:
        """Return Loguru-compatible log retention text."""
        return f"{max(int(self.log_retention_days), 1)} days"

    @property
    def normalized_api_base_url(self) -> str:
        """Return the externally visible API base URL without a trailing slash."""
        return self.api_base_url.rstrip("/")

    @property
    def upload_allowed_extension_list(self) -> list[str]:
        """Return normalized upload extension names without leading dots."""
        return [
            item.strip().lower().removeprefix(".")
            for item in self.upload_allowed_extensions.split(",")
            if item.strip()
        ]

    @property
    def upload_max_file_size(self) -> int:
        """Return upload size limit in bytes."""
        return max(int(self.upload_max_file_size_mb), 1) * 1024 * 1024

    @property
    def resolved_redis_url(self) -> str:
        """Return the canonical Redis URL derived from REDIS_URL or host fields."""
        if self.redis_url:
            return self.redis_url
        if not self.redis_host:
            return ""
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"

    @property
    def resolved_mysql_dsn(self) -> str:
        """Return the canonical MySQL DSN from DSN, URL, or split host fields."""
        if self.mysql_dsn:
            return self.mysql_dsn
        if self.mysql_url:
            return self.mysql_url
        if not self.mysql_host:
            return ""
        user = quote(self.mysql_user, safe="")
        password = quote(self.mysql_password, safe="")
        auth = ""
        if user and password:
            auth = f"{user}:{password}@"
        elif user:
            auth = f"{user}@"
        database = quote(self.mysql_database, safe="/")
        path = f"/{database}" if database else ""
        return f"mysql+pymysql://{auth}{self.mysql_host}:{self.mysql_port}{path}"

    @property
    def redis_instance_map(self) -> dict[str, str]:
        """Return REDIS_INSTANCES parsed as a name-to-URL map."""
        return _parse_instance_map(self.redis_instances)

    @property
    def mysql_instance_map(self) -> dict[str, str]:
        """Return MYSQL_INSTANCES parsed as a name-to-DSN map."""
        return _parse_instance_map(self.mysql_instances)


def _parse_instance_map(value: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(item) for key, item in payload.items() if item}


config = Settings()
