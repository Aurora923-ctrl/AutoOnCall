from app.services.rag_answer_coverage import (
    answer_topic_focus,
    build_answer_coverage_matrix,
    uncovered_answer_subgoals,
)


def test_answer_topic_focus_rejects_generic_answer() -> None:
    assert answer_topic_focus(
        "payment-service 的 pool_waiting 如何排查慢查询？",
        "检查证据并生成变更计划，审批后执行。",
    )["focused"] is False


def test_answer_topic_focus_accepts_incident_specific_answer() -> None:
    assert answer_topic_focus(
        "payment-service 的 pool_waiting 如何排查慢查询？",
        "检查 payment-service 的 pool_waiting 和慢查询，并对比 active_connections。",
    )["focused"] is True


def test_answer_topic_focus_recognizes_dependency_thread_and_queue_incidents() -> None:
    assert answer_topic_focus(
        "order-service 依赖 Redis 或 MQ 不可用，导致接口失败",
        "检查下游依赖超时和 MQ 错误。",
    )["focused"] is True
    assert answer_topic_focus(
        "active threads 达到上限且任务队列持续增长",
        "检查线程池 active threads 与 queue depth。",
    )["focused"] is True
    assert answer_topic_focus(
        "consumer lag 和 oldest message age 持续增长",
        "对比 consumer lag 与消息队列积压。",
    )["focused"] is True


def test_answer_coverage_matrix_binds_each_explicit_subgoal() -> None:
    matrix = build_answer_coverage_matrix(
        "Pod OOMKilled 后如何取证、区分泄漏和流量压力，并说明重启审批和当前事件边界？",
        [
            {
                "source_file": "memory_high_usage.md",
                "chunk_id": "memory_high_usage.md#summary",
                "heading_path": "快速决策摘要",
                "content": (
                    "先检查 memory_working_set 和 OOM 日志；区分内存泄漏与流量压力；"
                    "重启需要审批；当前 Incident 证据需要实时查询。"
                ),
            }
        ],
    )

    assert matrix["complete"] is True
    assert matrix["coverage_rate"] == 1.0
    assert {item["id"] for item in matrix["subgoals"]} == {
        "evidence",
        "diagnosis",
        "boundary",
        "temporal_boundary",
    }
    assert matrix["question_plan"] == {
        "domain": "memory",
        "explicit_entities": ("OOMKilled",),
        "max_claims": 3,
    }


def test_answer_coverage_matrix_reports_uncovered_subgoal() -> None:
    matrix = build_answer_coverage_matrix(
        "如何判断 Redis 原因，并说明回滚边界？",
        [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#1",
                "heading_path": "原因判别",
                "content": "根据 connected_clients 判断连接耗尽。",
            }
        ],
    )

    assert matrix["complete"] is False
    assert "boundary" in matrix["uncovered_subgoals"]


def test_answer_coverage_matrix_treats_operational_risk_queries_as_boundary_subgoals() -> None:
    matrix = build_answer_coverage_matrix(
        "写文件失败时如何区分 inode，并避免直接删除？",
        [
            {
                "source_file": "disk_high_usage.md",
                "chunk_id": "disk_high_usage.md#1",
                "heading_path": "首轮证据",
                "content": "只执行读取命令，不在诊断阶段直接删除或截断。",
            }
        ],
    )

    boundary = next(item for item in matrix["subgoals"] if item["id"] == "boundary")
    assert boundary["covered"] is True


def test_answer_coverage_matrix_does_not_treat_slow_query_as_action_boundary() -> None:
    matrix = build_answer_coverage_matrix(
        "payment-service 的 pool_waiting 和 active_connections 上升，如何排查慢查询？",
        [
            {
                "source_file": "payment_wiki.html",
                "chunk_id": "payment_wiki.html#approval",
                "heading_path": "Change and approval boundary",
                "content": "Adding an index or changing pool size requires human approval.",
            }
        ],
    )

    assert "boundary" not in {item["id"] for item in matrix["subgoals"]}


def test_uncovered_answer_subgoals_detects_missing_safety_and_history_boundaries() -> None:
    coverage = build_answer_coverage_matrix(
        "结合部署历史判断是否回滚",
        [
            {
                "source_file": "tickets.xlsx",
                "chunk_id": "tickets.xlsx#1",
                "content": "version rc3 risk_hint approval incident-window",
            }
        ],
    )

    missing = uncovered_answer_subgoals(
        "检查 rc3 的发布记录和 pool_waiting [证据 1]。",
        coverage,
    )

    assert "boundary" in missing
    assert "temporal_boundary" in missing


def test_uncovered_answer_subgoals_accepts_explicit_bounded_decision() -> None:
    coverage = build_answer_coverage_matrix(
        "结合部署历史判断是否回滚",
        [
            {
                "source_file": "tickets.xlsx",
                "chunk_id": "tickets.xlsx#1",
                "content": "version rc3 risk_hint approval incident-window",
            }
        ],
    )

    missing = uncovered_answer_subgoals(
        "对比历史发布记录与当前 incident-window 指标；回滚必须审批 [证据 1]。",
        coverage,
    )

    assert missing == []


def test_uncovered_answer_subgoals_does_not_treat_evidence_gap_as_boundary() -> None:
    coverage = build_answer_coverage_matrix(
        "Pod 发生 OOMKilled 后什么时候才能建议重启或扩容？",
        [
            {
                "source_file": "memory_high_usage.md",
                "chunk_id": "memory_high_usage.md#1",
                "content": "检查 OOM 日志；重启和扩容需要审批。",
            }
        ],
    )

    missing = uncovered_answer_subgoals(
        "检查 OOM 日志。[证据 1] 当前证据不足：现有片段未提供审批、pre-check、dry-run 或回滚验证边界。",
        coverage,
    )

    assert "boundary" in missing
