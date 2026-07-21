"""Lightweight dependency rules for the first core-file governance phase."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_foundation_modules_do_not_depend_on_api_agents_or_services() -> None:
    forbidden_prefixes = ("app.api", "app.agent", "app.services")
    violations: list[str] = []

    for relative_root in ("app/core", "app/integrations", "app/utils"):
        for path in (ROOT / relative_root).rglob("*.py"):
            forbidden = sorted(
                module for module in _imports(path) if module.startswith(forbidden_prefixes)
            )
            if forbidden:
                violations.append(f"{path.relative_to(ROOT)} imports {', '.join(forbidden)}")

    assert violations == []


def test_runtime_code_does_not_import_deprecated_read_models_compatibility_module() -> None:
    violations = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path != ROOT / "app/services/read_models.py"
        and "app.services.read_models" in _imports(path)
    ]

    assert violations == []


def test_store_consumers_use_factory_contract_instead_of_concrete_store_classes() -> None:
    consumers = [
        ROOT / "app/services/a2a_facade_core.py",
        ROOT / "app/services/alert_ingestion_service.py",
        ROOT / "app/services/approval_service.py",
        ROOT / "app/services/change_execution_service.py",
        ROOT / "app/services/report_generator.py",
        ROOT / "app/services/trace_service.py",
        ROOT / "app/services/aiops_service.py",
    ]

    for path in consumers:
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        assert "app.services.aiops_store" in imports, path.relative_to(ROOT)
        assert "AIOpsSQLiteStore" not in source, path.relative_to(ROOT)
        assert "AIOpsMySQLStore" not in source, path.relative_to(ROOT)


def test_aiops_read_model_package_does_not_depend_on_api_or_agent_layers() -> None:
    violations: list[str] = []
    for path in (ROOT / "app/services/aiops_read_models").rglob("*.py"):
        forbidden = sorted(
            module for module in _imports(path) if module.startswith(("app.api", "app.agent"))
        )
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)} imports {', '.join(forbidden)}")

    assert violations == []


def test_store_facades_delegate_schema_maintenance_and_import_responsibilities() -> None:
    sqlite_source = (ROOT / "app/services/sqlite_store.py").read_text(encoding="utf-8")
    mysql_source = (ROOT / "app/services/mysql_store.py").read_text(encoding="utf-8")

    assert "CREATE TABLE" not in sqlite_source
    assert "CREATE TABLE" not in mysql_source
    assert "DELETE FROM" not in sqlite_source
    assert "DELETE FROM" not in mysql_source
    assert "_insert_alert_event_for_import" not in mysql_source
    assert "initialize_sqlite_store(" in sqlite_source
    assert "initialize_mysql_store(" in mysql_source
    assert "cleanup_sqlite_runtime_data(" in sqlite_source
    assert "cleanup_mysql_runtime_data(" in mysql_source
    assert "import_mysql_runtime_state(" in mysql_source


def test_report_generator_delegates_builder_and_lifecycle_responsibilities() -> None:
    generator_path = ROOT / "app/services/report_generator.py"
    source = generator_path.read_text(encoding="utf-8")
    imports = _imports(generator_path)

    assert "app.services.report_builder" in imports
    assert "app.services.report_lifecycle" in imports
    assert "def _build_hypothesis_ranking(" not in source
    assert "def _build_conclusion_alignment(" not in source
    assert "def _upsert_change_execution_snapshot(" not in source
    assert "def _append_change_execution_summary(" not in source


def test_report_builder_and_lifecycle_do_not_depend_on_store_backends() -> None:
    forbidden = {
        "app.services.aiops_store",
        "app.services.sqlite_store",
        "app.services.mysql_store",
    }

    for relative_path in (
        "app/services/report_builder.py",
        "app/services/report_lifecycle.py",
    ):
        path = ROOT / relative_path
        assert _imports(path).isdisjoint(forbidden), relative_path


def test_aiops_service_delegates_run_and_resume_use_cases() -> None:
    path = ROOT / "app/services/aiops_service.py"
    source = path.read_text(encoding="utf-8")
    imports = _imports(path)

    assert "app.services.aiops_run" in imports
    assert "app.services.aiops_resume" in imports
    assert "self._run_use_case.execute(" in source
    assert "self._resume_use_case.execute(" in source


def test_replanner_delegates_decision_approval_and_recommendation_rules() -> None:
    replanner_imports = _imports(ROOT / "app/agent/aiops/replanner.py")
    analyzer_imports = _imports(ROOT / "app/agent/aiops/evidence_analyzer.py")
    fallback_imports = _imports(ROOT / "app/agent/aiops/plan_fallback.py")

    assert "replan_decision" in replanner_imports
    assert "replan_approval" in replanner_imports
    assert "evidence_recommendations" in analyzer_imports
    assert "fallback_scenarios" in fallback_imports


def test_stage5_services_delegate_generation_classification_and_projections() -> None:
    rag_policy_path = ROOT / "app/services/rag_answer_policy.py"
    feedback_path = ROOT / "app/services/feedback_service.py"
    change_path = ROOT / "app/services/change_execution_service.py"

    rag_source = rag_policy_path.read_text(encoding="utf-8")
    feedback_source = feedback_path.read_text(encoding="utf-8")
    change_source = change_path.read_text(encoding="utf-8")

    assert "app.services.rag_generation_context" in _imports(rag_policy_path)
    assert "def build_generation_evidence(" not in rag_source
    assert "def select_generation_excerpt(" not in rag_source

    assert "app.services.feedback_classification" in _imports(feedback_path)
    assert "def classify_improvement_items(" not in feedback_source
    assert "def infer_bad_case_category(" not in feedback_source

    assert "app.services.change_execution_projections" in _imports(change_path)
    assert "def _sync_execution_projections(" not in change_source
    assert "def _sync_execution_audit_projection(" not in change_source


def test_stage5_rule_modules_do_not_depend_on_persistence_backends() -> None:
    forbidden = {
        "app.services.aiops_store",
        "app.services.sqlite_store",
        "app.services.mysql_store",
    }
    for relative_path in (
        "app/services/rag_generation_context.py",
        "app/services/feedback_classification.py",
    ):
        path = ROOT / relative_path
        assert _imports(path).isdisjoint(forbidden), relative_path
