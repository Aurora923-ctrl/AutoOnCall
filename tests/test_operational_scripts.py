"""Contract tests for local operation scripts."""

from pathlib import Path

import pytest

from app import main as main_module
from app.config import Settings
from app.services.sqlite_store import AIOpsSQLiteStore
from scripts.maintenance import (
    cleanup_aiops_store,
    migrate_aiops_sqlite_to_mysql,
    reset_demo_data,
)
from scripts.maintenance.hygiene_check import find_hygiene_issues, main as hygiene_main

ROOT = Path(__file__).resolve().parents[1]


def test_makefile_separates_liveness_from_readiness_checks() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "HEALTH_LIVE_API = $(SERVER_URL)/health/live" in makefile
    assert "HEALTH_READY_API = $(SERVER_URL)/health/ready" in makefile
    assert "curl -s -f $(HEALTH_LIVE_API)" in makefile
    assert "curl -s -f $(HEALTH_READY_API)" in makefile


def test_windows_start_script_checks_live_before_ready_upload() -> None:
    script = (ROOT / "scripts" / "dev" / "start-windows.bat").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dev" / "pycharm_one_click_start.py").read_text(encoding="utf-8")

    live_index = launcher.index("LIVE_URL")
    ready_index = launcher.index("READY_URL")

    assert live_index < ready_index
    assert "pycharm_one_click_start.py %*" in script
    assert "wait_for_http(LIVE_URL" in launcher
    assert "wait_for_http(" in launcher
    assert "READY_URL, timeout_seconds=args.ready_timeout" in launcher
    assert '"curl",' in launcher
    assert '"--fail-with-body"' in launcher


def test_makefile_upload_fails_when_any_document_indexing_fails() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    upload = makefile.split("upload:", maxsplit=1)[1]
    upload = upload.split("# 列出文档", maxsplit=1)[0]

    assert "failed=$$((failed + 1))" in upload
    assert "文档上传或索引存在失败" in upload
    assert "exit 1" in upload


def test_production_docs_use_current_maintenance_script_paths() -> None:
    production = (ROOT / "deploy" / "production.md").read_text(encoding="utf-8")

    assert r"scripts\maintenance\cleanup_aiops_store.py" in production
    assert r"scripts\maintenance\migrate_aiops_sqlite_to_mysql.py" in production
    assert r"scripts\cleanup_aiops_store.py" not in production
    assert r"scripts\migrate_aiops_sqlite_to_mysql.py" not in production
    assert (
        "cleanup_aiops_store.py --database data\\aiops_state.db --keep-days 14 --execute"
        in production
    )
    assert "migrate_aiops_sqlite_to_mysql.py --sqlite data\\aiops_state.db --execute" in production


def test_mysql_init_provisions_writable_aiops_runtime_schema() -> None:
    runtime_schema = (
        ROOT / "deploy" / "adapters" / "mysql-init" / "002_aiops_runtime_store.sql"
    ).read_text(encoding="utf-8")

    for table in [
        "alert_events",
        "trace_events",
        "approval_requests",
        "change_executions",
        "aiops_sessions",
        "incident_states",
        "diagnosis_reports",
        "schema_migrations",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in runtime_schema

    assert "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX" in runtime_schema
    assert "ON autooncall.* TO 'autooncall'@'%'" in runtime_schema
    assert "INSERT IGNORE INTO schema_migrations" in runtime_schema


def test_destructive_maintenance_scripts_require_explicit_confirmation(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    class FakeStore:
        def cleanup_older_than(self, *, keep_days: int, dry_run: bool):
            return {"keep_days": keep_days, "dry_run": dry_run}

    monkeypatch.setattr(cleanup_aiops_store, "create_aiops_store", lambda _database: FakeStore())
    monkeypatch.setattr(
        cleanup_aiops_store,
        "parse_args",
        lambda: type(
            "Args",
            (),
            {"database": None, "keep_days": 14, "dry_run": False, "execute": False},
        )(),
    )
    assert cleanup_aiops_store.main() == 0
    assert '"dry_run": true' in capsys.readouterr().out.lower()

    monkeypatch.setattr(
        reset_demo_data,
        "parse_args",
        lambda: type(
            "Args",
            (),
            {
                "database": tmp_path / "reset.db",
                "backend": "sqlite",
                "quiet": True,
                "confirm_reset": False,
            },
        )(),
    )
    with pytest.raises(SystemExit, match="--confirm-reset"):
        reset_demo_data.main()


def test_migration_defaults_to_dry_run_without_execute(monkeypatch, tmp_path, capsys) -> None:
    database = tmp_path / "source.db"
    AIOpsSQLiteStore(database)
    monkeypatch.setattr(
        migrate_aiops_sqlite_to_mysql,
        "parse_args",
        lambda: type(
            "Args",
            (),
            {
                "sqlite": str(database),
                "mysql_dsn": "mysql+pymysql://user:password@localhost:3306/autooncall",
                "dry_run": False,
                "execute": False,
            },
        )(),
    )
    monkeypatch.setattr(
        migrate_aiops_sqlite_to_mysql,
        "AIOpsMySQLStore",
        lambda _dsn: (_ for _ in ()).throw(AssertionError("must not connect in dry run")),
    )

    assert migrate_aiops_sqlite_to_mysql.main() == 0
    assert '"dry_run": true' in capsys.readouterr().out.lower()


def test_production_docs_state_security_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    production = (ROOT / "deploy" / "production.md").read_text(encoding="utf-8")

    assert "## 安全边界" in readme
    assert "RBAC" in readme
    assert "CORS_ALLOWED_ORIGINS" in readme
    assert "AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=false" in readme
    assert "不自动执行重启、删 Pod、执行 SQL 或修改生产配置" in readme
    assert "SSO/OIDC or an internal admin token" in production
    assert "Add RBAC" in production


def test_container_delivery_files_exclude_local_runtime_artifacts() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    production = (ROOT / "deploy" / "production.md").read_text(encoding="utf-8")

    assert "FROM python:3.11.15-slim" in dockerfile
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "python -m pip install ." not in dockerfile
    assert "/health/live" in dockerfile
    assert 'os.environ[\\"PORT\\"]' in dockerfile
    assert "COPY app ./app" in dockerfile
    assert "COPY docs/knowledge-base ./docs/knowledge-base" in dockerfile
    assert "COPY static ./static" in dockerfile
    assert "COPY config ./config" in dockerfile
    assert "USER autooncall" in dockerfile
    assert '--host \\"${HOST}\\" --port \\"${PORT}\\"' in dockerfile

    for ignored_path in [
        ".env",
        "venv",
        "logs",
        "uploads",
        "data/*.db",
        "htmlcov",
    ]:
        assert ignored_path in dockerignore

    assert "容器入口只负责启动 FastAPI" in readme
    assert "This image is a delivery wrapper" in production


def test_logger_uses_central_runtime_config() -> None:
    logger_module = (ROOT / "app" / "utils" / "logger.py").read_text(encoding="utf-8")
    production = (ROOT / "deploy" / "production.md").read_text(encoding="utf-8")

    assert "retention=config.log_file_retention" in logger_module
    assert 'retention="7 days"' not in logger_module
    assert "diagnose=config.debug" in logger_module
    assert "SQLITE_BACKUP_ENABLED" not in production


def test_runtime_paths_are_loaded_from_central_config() -> None:
    upload_api = (ROOT / "app" / "api" / "file.py").read_text(encoding="utf-8")
    evaluations_api = (ROOT / "app" / "api" / "evaluations.py").read_text(encoding="utf-8")
    lexical_index = (ROOT / "app" / "services" / "lexical_index_service.py").read_text(
        encoding="utf-8"
    )

    assert "UPLOAD_DIR = Path(config.upload_dir)" in upload_api
    assert "ALLOWED_EXTENSIONS = config.upload_allowed_extension_list" in upload_api
    assert "MAX_FILE_SIZE = config.upload_max_file_size" in upload_api
    assert "return EVAL_SUMMARY_PATH or Path(config.eval_summary_path)" in evaluations_api
    assert "return EVAL_BACKLOG_PATH or Path(config.eval_backlog_path)" in evaluations_api
    assert (
        "return ADAPTER_VERIFICATION_PATH or Path(config.adapter_verification_path)"
        in evaluations_api
    )
    assert "DEFAULT_LEXICAL_INDEX_PATH = Path(config.rag_lexical_index_path)" in lexical_index


def test_makefile_exposes_hygiene_check_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "hygiene-check:" in makefile
    assert "scripts/maintenance/hygiene_check.py" in makefile


def test_makefile_exposes_demo_reports_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "demo-reports:" in makefile
    assert "scripts/demo/generate_demo_reports.py" in makefile


def test_seed_and_launcher_resets_require_explicit_confirmation() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    seed_script = (ROOT / "scripts" / "data" / "seed_demo_data.py").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dev" / "pycharm_one_click_start.py").read_text(encoding="utf-8")

    assert "scripts/data/seed_demo_data.py --no-reset" in makefile
    assert "--confirm-reset" in seed_script
    assert "--reset-demo-data" in launcher
    assert "--skip-demo-reset" not in launcher
    assert '"--confirm-reset"' in launcher


def test_pycharm_process_cleanup_uses_owned_pid_files() -> None:
    start_script = (ROOT / "scripts" / "dev" / "pycharm_one_click_start.py").read_text(
        encoding="utf-8"
    )
    stop_script = (ROOT / "scripts" / "dev" / "pycharm_one_click_stop.py").read_text(
        encoding="utf-8"
    )

    assert "pid_path.write_text(str(process.pid)" in start_script
    assert "stop_managed_process" in start_script
    assert "stop_processes_matching" not in start_script
    assert "MANAGED_PROCESSES" in stop_script
    assert "PROCESS_TOKENS" not in stop_script


def test_windows_wrappers_delegate_to_owned_pid_launchers() -> None:
    start_script = (ROOT / "scripts" / "dev" / "start-windows.bat").read_text(encoding="utf-8")
    stop_script = (ROOT / "scripts" / "dev" / "stop-windows.bat").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dev" / "pycharm_one_click_start.py").read_text(encoding="utf-8")

    assert "pycharm_one_click_start.py %*" in start_script
    assert "pycharm_one_click_stop.py %*" in stop_script
    assert "taskkill /FI" not in stop_script
    assert 'ROOT / ".venv" / "Scripts" / "python.exe"' in launcher
    assert "subprocess.CREATE_NO_WINDOW" in launcher


def test_knowledge_base_lives_under_docs() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    config = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "DOCS_DIR = docs/knowledge-base" in makefile
    assert 'index_allowed_roots: str = "uploads,docs/knowledge-base"' in config
    assert "docs/knowledge-base/" in readme


def test_makefile_exposes_api_contract_verifier_in_verify_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "api-contract-verify:" in makefile
    assert "reset-demo-data:" in makefile
    assert "scripts/maintenance/reset_demo_data.py --confirm-reset" in makefile
    assert "scripts/eval/verify_api_contracts.py" in makefile

    verify = makefile.split("verify:  ## 运行只验证门禁（不修改源码）", maxsplit=1)[1]
    verify = verify.split("check-all:", maxsplit=1)[0]
    assert "@$(MAKE) api-contract-verify" in verify


def test_readme_points_to_five_minute_interview_demo_and_core_stack() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    demo_doc = (ROOT / "docs" / "interview" / "five-minute-demo.md").read_text(encoding="utf-8")

    assert "docs/interview/five-minute-demo.md" in readme
    assert "Redis/MySQL 是 live adapter golden chain" in readme
    assert "K8s CrashLoop/OOMKilled 是 offline golden regression case" in readme
    assert "Milvus/RAG 是加分项" in demo_doc
    assert "make interview-up" in demo_doc
    assert "make sandbox-verify" in demo_doc
    assert "--env-file deploy\\sandbox.env" in demo_doc


def test_makefile_verify_runs_quality_gate_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    test_quick = makefile.split("test-quick:  ##", maxsplit=1)[1].split(
        "test-integrations:",
        maxsplit=1,
    )[0]
    verify = makefile.split("verify:  ## 运行只验证门禁（不修改源码）", maxsplit=1)[1]
    verify = verify.split("check-all:", maxsplit=1)[0]

    assert "--no-cov" in test_quick
    assert "@$(MAKE) format-check" in verify
    assert "@$(MAKE) lint" in verify
    assert "@$(MAKE) type-check" in verify
    assert "@$(MAKE) security" in verify
    assert "@$(MAKE) test-quick" in verify
    assert "@$(MAKE) eval-rag" in verify
    assert "@$(MAKE) eval-ragas" not in verify
    assert "@$(MAKE) api-contract-verify" in verify
    assert "@$(MAKE) reference-check" in verify
    assert "@$(MAKE) hygiene-check" in verify


def test_makefile_uses_stable_rag_contract_and_explicit_candidate_promotion() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    eval_rag = makefile.split("eval-rag:  ##", maxsplit=1)[1].split(
        "eval-ragas:",
        maxsplit=1,
    )[0]
    assert "--cases eval/rag_cases.yaml" in eval_rag
    assert "--cases eval/rag_relevance_cases.yaml" not in eval_rag
    assert "candidate-baseline:" in makefile
    assert "run_benchmark_baseline.py --candidate" in makefile


def test_makefile_check_all_is_verify_alias() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    check_all = makefile.split("check-all:  ## 兼容入口：等同 make verify", maxsplit=1)[1]
    check_all = check_all.split("pre-commit-install:", maxsplit=1)[0]

    assert "@$(MAKE) verify" in check_all


def test_hygiene_check_detects_generated_artifacts(tmp_path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "aiops_state.db").write_text("", encoding="utf-8")
    (tmp_path / "app" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".coverage").write_text("", encoding="utf-8")
    (tmp_path / ".git" / "logs").mkdir(parents=True)

    issues = find_hygiene_issues(tmp_path)
    issue_paths = {issue.path for issue in issues}

    assert "logs" in issue_paths
    assert "data/aiops_state.db" in issue_paths
    assert "app/__pycache__" in issue_paths
    assert ".coverage" in issue_paths
    assert not any(issue.path.startswith(".git/") for issue in issues)
    assert hygiene_main(["--root", str(tmp_path), "--json"]) == 1


def test_hygiene_check_respects_gitignore_for_tracked_generated_dirs() -> None:
    assert not any(issue.path == "logs" for issue in find_hygiene_issues(ROOT))


def test_hygiene_check_passes_clean_repository_tree(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    assert find_hygiene_issues(tmp_path) == []
    assert hygiene_main(["--root", str(tmp_path)]) == 0


def test_production_exposure_warnings_for_open_demo_defaults(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "0.0.0.0")
    monkeypatch.setattr(main_module.config, "debug", True)
    monkeypatch.setattr(main_module.config, "api_auth_enabled", False)
    monkeypatch.setattr(main_module.config, "cors_allowed_origins", "*")
    monkeypatch.setattr(main_module.config, "aiops_mock_fallback_enabled", True)

    warnings = main_module.production_exposure_warnings()

    assert "debug mode is enabled while binding to a non-local host" in warnings
    assert "API auth is disabled while binding to a non-local host" in warnings
    assert "CORS allows all origins while binding to a non-local host" in warnings
    assert "AIOps mock fallback is enabled while binding to a non-local host" in warnings


def test_production_exposure_strict_mode_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "0.0.0.0")
    monkeypatch.setattr(main_module.config, "debug", False)
    monkeypatch.setattr(main_module.config, "api_auth_enabled", False)
    monkeypatch.setattr(main_module.config, "cors_allowed_origins", "*")
    monkeypatch.setattr(main_module.config, "production_exposure_strict", True)

    with pytest.raises(RuntimeError, match="Unsafe production exposure configuration"):
        main_module.enforce_production_exposure_policy()


def test_production_exposure_rejects_enabled_auth_without_usable_tokens(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "0.0.0.0")
    monkeypatch.setattr(main_module.config, "debug", False)
    monkeypatch.setattr(main_module.config, "api_auth_enabled", True)
    monkeypatch.setattr(main_module.config, "api_read_token", "replace-with-read-token")
    monkeypatch.setattr(main_module.config, "api_operator_token", "")
    monkeypatch.setattr(main_module.config, "api_approver_token", "")
    monkeypatch.setattr(main_module.config, "api_admin_token", "")
    monkeypatch.setattr(main_module.config, "api_auth_tokens", "")

    assert (
        "API auth has no usable tokens while binding to a non-local host"
        in main_module.production_exposure_warnings()
    )


def test_production_exposure_warnings_ignore_local_bind(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "127.0.0.1")
    monkeypatch.setattr(main_module.config, "debug", True)
    monkeypatch.setattr(main_module.config, "api_auth_enabled", False)
    monkeypatch.setattr(main_module.config, "cors_allowed_origins", "*")
    monkeypatch.setattr(main_module.config, "aiops_mock_fallback_enabled", True)

    assert main_module.production_exposure_warnings() == []


@pytest.mark.parametrize(
    ("host", "externally_bound"),
    [
        ("127.0.0.1", False),
        ("::1", False),
        ("localhost", False),
        ("0.0.0.0", True),
        ("::", True),
        ("192.168.1.20", True),
        ("autooncall.internal", True),
        ("", True),
    ],
)
def test_external_bind_detection_covers_non_loopback_hosts(
    host: str,
    externally_bound: bool,
) -> None:
    assert main_module.is_externally_bound_host(host) is externally_bound


def test_empty_cors_configuration_does_not_fall_back_to_wildcard() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins="")

    assert settings.cors_origins == []


def test_cors_configuration_normalizes_duplicates_and_trailing_slashes() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="https://ops.example/, https://ops.example, http://localhost:9900/",
    )

    assert settings.cors_origins == ["https://ops.example", "http://localhost:9900"]


def test_embedding_batch_size_normalizes_legacy_values_to_provider_limit() -> None:
    settings = Settings(_env_file=None, dashscope_embedding_batch_size=64)
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert settings.dashscope_embedding_batch_size == 10
    assert "DASHSCOPE_EMBEDDING_BATCH_SIZE=10" in env_example


def test_env_example_matches_local_adapter_ports_and_has_unique_settings() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    setting_names = [
        line.split("=", 1)[0]
        for line in env_example.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]

    assert "REDIS_PORT=16379" in env_example
    assert "redis://127.0.0.1:16379/0" in env_example
    assert "MYSQL_PORT=13306" in env_example
    assert "@127.0.0.1:13306/autooncall" in env_example
    assert len(setting_names) == len(set(setting_names))
    assert setting_names.count("TICKET_API_URL") == 1
    assert setting_names.count("TICKET_API_BEARER_TOKEN") == 1
    assert setting_names.count("TICKET_API_TIMEOUT_SECONDS") == 1


def test_static_assets_are_resolved_from_the_repository_root() -> None:
    assert main_module.STATIC_DIR == ROOT / "static"
    assert (main_module.STATIC_DIR / "index.html").exists()


def test_production_app_disables_interactive_api_documentation() -> None:
    assert main_module.config.debug is False
    assert main_module.app.docs_url is None
    assert main_module.app.redoc_url is None
    assert main_module.app.openapi_url is None


def test_cors_does_not_enable_cookie_credentials() -> None:
    cors_middleware = next(
        middleware
        for middleware in main_module.app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )

    assert cors_middleware.kwargs["allow_credentials"] is False


@pytest.mark.asyncio
async def test_lifespan_closes_milvus_when_application_body_fails(monkeypatch) -> None:
    closed = False

    def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(main_module, "enforce_production_exposure_policy", lambda: None)
    monkeypatch.setattr(main_module.milvus_manager, "close", close)

    with pytest.raises(RuntimeError, match="lifespan body failed"):
        async with main_module.lifespan(main_module.app):
            raise RuntimeError("lifespan body failed")

    assert closed is True


@pytest.mark.asyncio
async def test_lifespan_closes_core_milvus_when_vector_store_close_fails(monkeypatch) -> None:
    closed = False

    async def fail_vector_store_close() -> None:
        raise RuntimeError("vector store close failed")

    def close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(main_module, "enforce_production_exposure_policy", lambda: None)
    monkeypatch.setattr(main_module.vector_store_manager, "aclose", fail_vector_store_close)
    monkeypatch.setattr(main_module.milvus_manager, "close", close)

    with pytest.raises(RuntimeError, match="vector store close failed"):
        async with main_module.lifespan(main_module.app):
            pass

    assert closed is True
