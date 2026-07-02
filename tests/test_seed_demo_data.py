import json

from app.services.sqlite_store import AIOpsSQLiteStore
from scripts.data.seed_demo_data import DEMO_EVAL_CASE_IDS, seed_demo_data


def test_seed_demo_data_writes_replay_store_and_artifacts(tmp_path):
    database = tmp_path / "aiops_state.db"
    eval_summary = tmp_path / "eval_summary.json"
    adapter_summary = tmp_path / "adapter_summary.json"

    result = seed_demo_data(
        database_path=database,
        eval_summary_path=eval_summary,
        adapter_summary_path=adapter_summary,
    )

    assert result["incident_count"] == 4
    assert database.exists()
    assert eval_summary.exists()
    assert eval_summary.with_suffix(".md").exists()
    assert adapter_summary.exists()

    store = AIOpsSQLiteStore(database)
    reports = {report.incident_id: report for report in store.list_latest_reports()}
    assert set(DEMO_EVAL_CASE_IDS).issubset(reports)

    redis_report = reports["INC-REDIS-001"]
    assert redis_report.approval_status == "approved"
    assert redis_report.change_plan["change_plan_id"] == "change-plan-demo-redis"
    assert redis_report.change_executions[0]["status"] == "dry_run_completed"

    redis_events = store.list_trace_events(incident_id="INC-REDIS-001")
    assert any(event.event_type == "replan" for event in redis_events)
    assert any(event.event_type == "approval_requested" for event in redis_events)

    approvals = store.list_approval_requests(incident_id="INC-REDIS-001")
    assert approvals[0].status == "approved"
    changes = store.list_change_executions(incident_id="INC-REDIS-001")
    assert changes[0].status == "dry_run_completed"

    summary = json.loads(eval_summary.read_text(encoding="utf-8"))
    assert summary["all_passed"] is True
    assert {case["id"] for case in summary["cases"]} == set(DEMO_EVAL_CASE_IDS.values())
    assert summary["metrics"]["trace_completeness"]["pass_rate"] == 1.0

    adapter = json.loads(adapter_summary.read_text(encoding="utf-8"))
    assert adapter["available"] is True
    assert adapter["status"] == "passed"
    assert "prometheus" in adapter["data_sources"]


def test_seed_demo_data_is_idempotent(tmp_path):
    database = tmp_path / "aiops_state.db"
    eval_summary = tmp_path / "eval_summary.json"
    adapter_summary = tmp_path / "adapter_summary.json"

    seed_demo_data(
        database_path=database,
        eval_summary_path=eval_summary,
        adapter_summary_path=adapter_summary,
    )
    store = AIOpsSQLiteStore(database)
    first_event_count = len(store.list_trace_events(incident_id="INC-REDIS-001"))
    first_report_count = len(store.list_latest_reports())

    seed_demo_data(
        database_path=database,
        eval_summary_path=eval_summary,
        adapter_summary_path=adapter_summary,
    )

    assert len(store.list_trace_events(incident_id="INC-REDIS-001")) == first_event_count
    assert len(store.list_latest_reports()) == first_report_count
