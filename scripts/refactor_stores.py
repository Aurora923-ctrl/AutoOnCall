"""Mechanical splitter for the legacy SQLite/MySQL store modules.

This script is intentionally kept small and deterministic. It extracts the current
method bodies without rewriting them, so transaction and exception behavior remain
byte-for-byte equivalent during the package move.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "app" / "services"
STORES = SERVICES / "stores"

METHOD_GROUPS = {
    "alerts": {
        "save_alert_event",
        "persist_alert_ingestion",
        "claim_alert_auto_diagnosis",
        "release_alert_auto_diagnosis",
        "get_alert_event",
        "list_alert_events",
        "_insert_alert_event_if_absent",
        "_save_alert_event",
    },
    "traces": {"save_trace_event", "list_trace_events"},
    "approvals": {
        "save_approval_request",
        "create_approval_request_once",
        "save_approval_decision_if_pending",
        "get_approval_request",
        "list_approval_requests",
    },
    "executions": {
        "save_change_execution",
        "save_change_execution_if_status",
        "create_change_execution_once",
        "get_change_execution",
        "list_change_executions",
    },
    "sessions": {
        "save_aiops_session_snapshot",
        "save_aiops_session_snapshot_with_incident",
        "create_aiops_session_snapshot_with_incident",
        "update_aiops_session_snapshot_with_incident_if_status",
        "create_aiops_session_snapshot",
        "update_aiops_session_snapshot_if_status",
        "get_aiops_session_snapshot",
        "get_latest_aiops_session_snapshot",
        "list_aiops_session_snapshots",
    },
    "a2a_tasks": {
        "create_a2a_task_record",
        "save_a2a_task_record",
        "get_a2a_task_record",
        "list_a2a_task_records",
    },
    "incidents": {
        "save_incident_state",
        "get_incident_state",
        "list_incident_states",
        "_save_incident_state",
    },
    "reports": {
        "save_report",
        "save_report_with_incident",
        "get_report",
        "get_latest_report",
        "list_latest_reports",
    },
    "runtime_import": {
        "import_runtime_state",
        "_find_runtime_import_conflicts",
        "_insert_alert_event_for_import",
        "_insert_trace_event_for_import",
        "_insert_approval_request_for_import",
        "_insert_change_execution_for_import",
        "_insert_aiops_session_for_import",
        "_insert_incident_state_for_import",
        "_insert_report_for_import",
    },
    "retention": {
        "reset_runtime_data",
        "cleanup_older_than",
        "_select_retention_eligible_incidents",
        "_count_retention_incidents",
        "_delete_retention_incidents",
    },
    "schema": {
        "_initialize",
        "_apply_sqlite_approval_and_change_idempotency",
        "_count_change_execution_scope_duplicates",
        "_ensure_approval_idempotency_columns",
        "_apply_mysql_approval_and_change_idempotency",
        "_ensure_retention_timestamp_columns",
        "_ensure_runtime_column_capacities",
        "_require_transactional_runtime_tables",
        "_ensure_change_execution_scope_unique_index",
        "_record_migration_warning",
    },
    "connection": {"_connect"},
}


def source_segment(lines: list[str], node: ast.AST) -> str:
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def method_segment(lines: list[str], node: ast.AST) -> str:
    text = source_segment(lines, node)
    return "\n".join(line[4:] if line.startswith("    ") else line for line in text.splitlines())


def header(source: str, tree: ast.Module) -> str:
    lines = source.splitlines(keepends=True)
    imports = [
        source_segment(lines, node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    return (
        '"""Domain repository extracted from the legacy AIOps store."""\n\n'
        "# ruff: noqa: F401\n\n"
        + "".join(imports)
        + "\n"
        + "from app.services.stores.common.serialization import dump_model as _dump_model\n"
        + "from app.services.stores.common.serialization import load_payload as _load_payload\n"
    )


def assignment_segment(source: str, tree: ast.Module, name: str) -> str:
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return source_segment(lines, node)
    return ""


def split_backend(backend: str) -> None:
    legacy_path = SERVICES / f"{backend}_store.py"
    source = legacy_path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    class_name = "AIOpsSQLiteStore" if backend == "sqlite" else "AIOpsMySQLStore"
    mixins: list[str] = []
    backend_dir = STORES / backend
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "__init__.py").write_text("", encoding="utf-8")

    group_order = [
        "alerts",
        "traces",
        "approvals",
        "executions",
        "sessions",
        "a2a_tasks",
        "incidents",
        "reports",
    ]
    if backend == "mysql":
        group_order.append("runtime_import")
    group_order.extend(["retention", "schema", "connection"])

    for group in group_order:
        selected = [methods[name] for name in methods if name in METHOD_GROUPS[group]]
        if not selected:
            continue
        module_name = "maintenance" if group == "retention" else group
        mixin_name = "".join(part.title() for part in module_name.split("_")) + "Mixin"
        mixins.append(mixin_name)
        body = header(source, tree)
        if group == "retention":
            body += "\n" + assignment_segment(source, tree, "_RETENTION_SQL")
            body += "\n" + assignment_segment(source, tree, "_RUNTIME_RESET_SQL")
        body += f"\n\nclass {mixin_name}:\n"
        for node in selected:
            method = method_segment(lines, node)
            body += "\n" + "\n".join(f"    {line}" if line else "" for line in method.splitlines())
            body += "\n"
        (backend_dir / f"{module_name}.py").write_text(body, encoding="utf-8")

    init_node = methods["__init__"]
    init_text = method_segment(lines, init_node)
    imports = "\n".join(
        f"from app.services.stores.{backend}.{('maintenance' if group == 'retention' else group)} "
        f"import {''.join(part.title() for part in ('maintenance' if group == 'retention' else group).split('_'))}Mixin"
        for group in group_order
        if any(name in methods for name in METHOD_GROUPS[group])
    )
    helper_exports = ""
    if backend == "sqlite":
        helper_exports = (
            "from app.services.stores.sqlite.connection import resolve_sqlite_path\n"
        )
    else:
        helper_exports = (
            "from app.services.stores.mysql.connection import (\n"
            "    close_mysql_pools,\n"
            "    parse_mysql_dsn as _parse_mysql_dsn,\n"
            "    redact_mysql_dsn as _redact_mysql_dsn,\n"
            ")\n"
        )
    facade = (
        f'"""{backend.title()} compatibility facade for domain repositories."""\n\n'
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n\n"
        "from app.config import config\n"
        f"{imports}\n"
        f"{helper_exports}\n\n"
        f"class {class_name}(\n"
        + "".join(f"    {name},\n" for name in mixins)
        + "):\n"
        '    """Backward-compatible facade composed from domain repositories."""\n\n'
        + "\n".join(f"    {line}" if line else "" for line in init_text.splitlines())
        + "\n"
    )
    legacy_path.write_text(facade, encoding="utf-8")


def patch_connection_modules() -> None:
    sqlite_path = STORES / "sqlite" / "connection.py"
    sqlite_source = sqlite_path.read_text(encoding="utf-8")
    sqlite_source = sqlite_source.replace(
        "class ConnectionMixin:",
        "def resolve_sqlite_path(storage_path: str | Path | None = None) -> Path:\n"
        '    """Resolve a runtime storage path to a SQLite database path."""\n'
        "    if storage_path is None:\n"
        "        return Path(config.aiops_sqlite_path)\n"
        "    path = Path(storage_path)\n"
        '    if path.suffix.lower() == \".jsonl\":\n'
        '        return path.with_suffix(\".db\")\n'
        "    return path\n\n\n"
        "class ConnectionMixin:",
    )
    sqlite_path.write_text(sqlite_source, encoding="utf-8")

    mysql_path = STORES / "mysql" / "connection.py"
    mysql_source = mysql_path.read_text(encoding="utf-8")
    mysql_source = mysql_source.replace(
        "class ConnectionMixin:",
        "_MYSQL_POOLS: dict[tuple[tuple[str, Any], ...], Any] = {}\n"
        "_MYSQL_POOLS_LOCK = Lock()\n\n\n"
        "def close_mysql_pools() -> None:\n"
        '    \"\"\"Close process-wide MySQL pools during application shutdown.\"\"\"\n'
        "    with _MYSQL_POOLS_LOCK:\n"
        "        pools = list(_MYSQL_POOLS.values())\n"
        "        _MYSQL_POOLS.clear()\n"
        "    for pool in pools:\n"
        "        pool.close()\n\n\n"
        "def parse_mysql_dsn(dsn: str) -> dict[str, Any]:\n"
        "    parsed = urlparse(dsn)\n"
        '    if parsed.scheme not in {\"mysql\", \"mysql+pymysql\"}:\n'
        '        raise ValueError(\"MySQL DSN must start with mysql:// or mysql+pymysql://\")\n'
        "    query = parse_qs(parsed.query)\n"
        "    settings: dict[str, Any] = {\n"
        '        \"host\": parsed.hostname or \"localhost\",\n'
        '        \"port\": parsed.port or 3306,\n'
        '        \"user\": unquote(parsed.username or \"\"),\n'
        '        \"password\": unquote(parsed.password or \"\"),\n'
        '        \"database\": unquote(parsed.path.lstrip(\"/\")),\n'
        '        \"charset\": query.get(\"charset\", [\"utf8mb4\"])[0],\n'
        '        \"connect_timeout\": int(float(query.get(\"connect_timeout\", [config.mysql_timeout_seconds])[0])),\n'
        '        \"read_timeout\": int(float(query.get(\"read_timeout\", [config.mysql_timeout_seconds])[0])),\n'
        '        \"write_timeout\": int(float(query.get(\"write_timeout\", [config.mysql_timeout_seconds])[0])),\n'
        "    }\n"
        '    if not settings[\"database\"]:\n'
        '        raise ValueError(\"MySQL DSN must include a database name\")\n'
        "    return settings\n\n\n"
        "def redact_mysql_dsn(dsn: str) -> str:\n"
        "    parsed = urlparse(dsn)\n"
        "    if parsed.password is None:\n"
        "        return dsn\n"
        '    auth = f\"{parsed.username or \'\'}:***@\"\n'
        '    port = f\":{parsed.port}\" if parsed.port else \"\"\n'
        '    return f\"{parsed.scheme}://{auth}{parsed.hostname or \'\'}{port}{parsed.path}\"\n\n\n'
        "class ConnectionMixin:",
    )
    mysql_path.write_text(mysql_source, encoding="utf-8")


def main() -> None:
    (STORES / "common").mkdir(parents=True, exist_ok=True)
    (STORES / "__init__.py").write_text("", encoding="utf-8")
    (STORES / "common" / "__init__.py").write_text("", encoding="utf-8")
    split_backend("sqlite")
    split_backend("mysql")
    patch_connection_modules()


if __name__ == "__main__":
    main()
