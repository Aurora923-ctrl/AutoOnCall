"""Contract tests for local operation scripts."""

from pathlib import Path

from app import main as main_module
from scripts.hygiene_check import find_hygiene_issues, main as hygiene_main

ROOT = Path(__file__).resolve().parents[1]


def test_makefile_separates_liveness_from_readiness_checks() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "HEALTH_LIVE_API = $(SERVER_URL)/health/live" in makefile
    assert "HEALTH_READY_API = $(SERVER_URL)/health/ready" in makefile
    assert "curl -s -f $(HEALTH_LIVE_API)" in makefile
    assert "curl -s -f $(HEALTH_READY_API)" in makefile


def test_windows_start_script_checks_live_before_ready_upload() -> None:
    script = (ROOT / "start-windows.bat").read_text(encoding="utf-8")

    live_index = script.index("http://localhost:9900/health/live")
    ready_index = script.index("http://localhost:9900/health/ready")

    assert live_index < ready_index
    assert "FastAPI 进程可能还未启动" in script
    assert "依赖尚未就绪，跳过文档上传" in script


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
    assert "EVAL_SUMMARY_PATH = Path(config.eval_summary_path)" in evaluations_api
    assert "ADAPTER_VERIFICATION_PATH = Path(config.adapter_verification_path)" in evaluations_api
    assert "DEFAULT_LEXICAL_INDEX_PATH = Path(config.rag_lexical_index_path)" in lexical_index


def test_makefile_exposes_hygiene_check_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "hygiene-check:" in makefile
    assert "scripts/hygiene_check.py" in makefile


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


def test_hygiene_check_passes_clean_repository_tree(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    assert find_hygiene_issues(tmp_path) == []
    assert hygiene_main(["--root", str(tmp_path)]) == 0


def test_production_exposure_warnings_for_open_demo_defaults(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "0.0.0.0")
    monkeypatch.setattr(main_module.config, "api_auth_enabled", False)
    monkeypatch.setattr(main_module.config, "cors_allowed_origins", "*")

    warnings = main_module.production_exposure_warnings()

    assert "API auth is disabled while binding to a non-local host" in warnings
    assert "CORS allows all origins while binding to a non-local host" in warnings


def test_production_exposure_warnings_ignore_local_bind(monkeypatch) -> None:
    monkeypatch.setattr(main_module.config, "host", "127.0.0.1")
    monkeypatch.setattr(main_module.config, "api_auth_enabled", False)
    monkeypatch.setattr(main_module.config, "cors_allowed_origins", "*")

    assert main_module.production_exposure_warnings() == []
