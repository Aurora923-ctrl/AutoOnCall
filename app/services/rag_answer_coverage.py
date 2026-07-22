"""Answer coverage planning for grounded RAG generation."""

from __future__ import annotations

from typing import Any

from app.services.rag_question_plan import build_question_plan

_SUBGOAL_DETAILS: dict[str, tuple[str, tuple[str, ...]]] = {
    "evidence": (
        "需要哪些证据",
        ("证据", "检查", "排查", "验证", "指标", "日志", "命令", "working_set", "oom"),
    ),
    "diagnosis": (
        "需要哪些判断",
        ("区分", "判断", "原因", "定位", "根因", "泄漏", "流量", "压力", "risk_hint"),
    ),
    "alert_design": (
        "需要怎样设计症状告警",
        ("告警", "症状", "用户可见", "用户影响", "低噪声", "可行动", "alert", "symptom"),
    ),
    "boundary": (
        "是否要求处置边界",
        ("边界", "审批", "重启", "扩容", "限流", "回滚", "删除", "清理", "只执行读取"),
    ),
    "temporal_boundary": (
        "是否要求区分历史与当前事件",
        ("历史", "当前", "实时", "incident-window", "事故窗口", "工单", "部署历史", "version", "postmortem"),
    ),
}


def build_answer_coverage_matrix(
    query: str,
    chunks: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Plan query subgoals and bind each explicit subgoal to retrieved chunks."""
    text = str(query or "").strip().lower()
    question_plan = build_question_plan(query)
    normalized_chunks = [chunk for chunk in chunks or [] if isinstance(chunk, dict)]
    subgoals: list[dict[str, Any]] = []
    for planned_subgoal in question_plan.subgoals:
        key = planned_subgoal.id
        label, support_markers = _SUBGOAL_DETAILS.get(key, (key, ()))
        bindings = []
        for chunk in normalized_chunks:
            source_file = str(chunk.get("source_file") or "").lower()
            searchable = " ".join(
                [
                    str(chunk.get("source_file") or ""),
                    str(chunk.get("heading_path") or ""),
                    str(chunk.get("content") or chunk.get("content_preview") or ""),
                ]
            ).lower()
            overlap = [marker for marker in support_markers if marker.lower() in searchable]
            if overlap or _heading_matches(key, searchable):
                bindings.append(
                    {
                        "source_file": str(chunk.get("source_file") or ""),
                        "chunk_id": str(chunk.get("chunk_id") or ""),
                        "matched_terms": overlap[:8],
                        "binding_reason": "content_or_heading",
                    }
                )
            elif _topic_matches(text, source_file, key):
                bindings.append(
                    {
                        "source_file": str(chunk.get("source_file") or ""),
                        "chunk_id": str(chunk.get("chunk_id") or ""),
                        "matched_terms": [],
                        "binding_reason": "query_topic_source",
                    }
                )
        subgoals.append(
            {
                "id": key,
                "label": label,
                "required": True,
                "bound_chunks": bindings[:5],
                "covered": bool(bindings),
            }
        )
    required = len(subgoals)
    covered = sum(1 for item in subgoals if item["covered"])
    return {
        "query": str(query or ""),
        "subgoals": subgoals,
        "required_count": required,
        "covered_count": covered,
        "coverage_rate": round(covered / required, 4) if required else 1.0,
        "complete": covered == required,
        "uncovered_subgoals": [item["id"] for item in subgoals if not item["covered"]],
        "question_plan": {
            "domain": question_plan.domain,
            "explicit_entities": question_plan.explicit_entities,
            "max_claims": question_plan.max_claims,
        },
    }


def uncovered_answer_subgoals(
    answer: str,
    coverage: dict[str, Any] | None,
) -> list[str]:
    """Return matrix subgoals that the generated answer still omitted."""
    text = str(answer or "").lower()
    if not text or not isinstance(coverage, dict):
        return []
    subgoals = coverage.get("subgoals")
    if not isinstance(subgoals, list):
        return []

    markers = {
        "evidence": (
            "检查",
            "确认",
            "对比",
            "保留",
            "证据",
            "指标",
            "日志",
            "命令",
            "check",
            "confirm",
        ),
        "diagnosis": (
            "区分",
            "判断",
            "对比",
            "原因",
            "相关性",
            "不能直接",
            "不能单独",
            "distinguish",
            "compare",
        ),
        "alert_design": (
            "告警",
            "症状",
            "用户可见",
            "用户影响",
            "低噪声",
            "可行动",
            "alert",
            "symptom",
            "user-visible",
        ),
        "boundary": (
            "审批",
            "dry-run",
            "回滚",
            "只执行读取",
            "不直接",
            "禁止直接",
            "人工",
            "approval",
            "rollback",
        ),
        "temporal_boundary": (
            "历史",
            "当前",
            "实时",
            "incident-window",
            "不能替代",
            "证据不足",
            "retrospective",
        ),
    }
    missing: list[str] = []
    for item in subgoals:
        if not isinstance(item, dict):
            continue
        subgoal_id = str(item.get("id") or "")
        expected = markers.get(subgoal_id, ())
        if subgoal_id == "boundary":
            # A generic evidence-gap sentence is not an operational boundary.
            # Answers to action-oriented questions must say what is allowed,
            # what requires approval, or what rollback/verification is required.
            affirmative_text = " ".join(
                line
                for line in text.splitlines()
                if "当前证据不足" not in line
            )
            boundary_terms = (
                "审批",
                "approval",
                "dry-run",
                "回滚",
                "rollback",
                "只读",
                "只执行读取",
                "人工接管",
                "人工审批",
                "不得直接",
                "不直接执行",
                "执行前",
            )
            if not any(marker in affirmative_text for marker in boundary_terms):
                missing.append(subgoal_id)
            continue
        if expected and not any(marker in text for marker in expected):
            missing.append(subgoal_id)
    return missing


def answer_topic_focus(query: str, answer: str) -> dict[str, Any]:
    """Measure whether a grounded answer stays on the incident topic."""
    query_terms = set(_topic_terms(str(query or "")))
    answer_terms = set(_topic_terms(str(answer or "")))
    matched = sorted(query_terms & answer_terms)
    missing = sorted(query_terms - answer_terms)
    return {
        "query_terms": sorted(query_terms),
        "matched_terms": matched,
        "missing_terms": missing,
        "focused": not query_terms or bool(matched),
    }


def _topic_terms(text: str) -> list[str]:
    normalized = str(text or "").lower()
    aliases = {
        "cpu": ("cpu", "进程", "线程"),
        "memory": ("oom", "内存", "rss", "gc"),
        "disk": ("磁盘", "inode", "文件", "目录"),
        "redis": ("redis", "maxclients", "connected_clients"),
        "mysql": ("mysql", "慢查询", "sql", "pool_waiting", "active_connections"),
        "kubernetes": ("kubernetes", "pod", "service", "endpointslice"),
        "loki": ("loki", "discarded"),
        "dns": ("dns", "解析", "nxdomain", "servfail"),
        "network": ("网络", "超时", "snat", "连接"),
        "dependency": ("依赖", "下游", "upstream", "downstream", "mq"),
        "thread_pool": (
            "thread pool",
            "active threads",
            "线程池",
            "任务队列",
            "queue depth",
        ),
        "message_queue": (
            "consumer lag",
            "oldest message age",
            "消息队列",
            "积压",
            "幂等",
        ),
        "history": ("历史", "工单", "部署", "发布", "incident-window"),
        "approval": ("审批", "回滚", "dry-run", "变更"),
        "alert": ("告警", "症状", "用户可见", "alert", "symptom"),
    }
    return [key for key, markers in aliases.items() if any(marker in normalized for marker in markers)]


def _heading_matches(key: str, searchable: str) -> bool:
    markers = {
        "evidence": ("首轮证据", "排查步骤", "常用命令", "指标"),
        "diagnosis": ("原因判别", "故障定位", "根因"),
        "alert_design": ("告警", "症状", "用户可见", "alert", "symptom"),
        "boundary": ("处置计划", "升级与审批", "回滚条件", "审批"),
        "temporal_boundary": ("incident-window", "历史", "当前", "工单"),
    }
    return any(marker.lower() in searchable for marker in markers.get(key, ()))


def _topic_matches(query: str, source_file: str, subgoal: str) -> bool:
    """Bind an explicit subgoal to a topic-specific chunk when wording differs."""
    topics = {
        "memory": ("memory", "oom", "oomkilled", "jvm", "内存"),
        "disk": ("disk", "inode", "filesystem", "磁盘"),
        "redis": ("redis", "maxclients", "connected_clients"),
        "mysql": ("mysql", "sql", "pool_waiting", "active_connections", "payment"),
        "kubernetes": ("kubernetes", "k8s", "pod", "service", "endpointslice"),
        "loki": ("loki", "discarded", "ingestion", "日志"),
        "ticket": ("inc-", "ticket", "工单", "历史", "部署", "发布"),
        "service": ("503", "5xx", "依赖", "下游", "不可用"),
    }
    topic_hit = any(
        any(marker in query for marker in markers)
        and any(marker in source_file for marker in markers)
        for markers in topics.values()
    )
    if not topic_hit:
        return False
    if subgoal == "temporal_boundary":
        return any(
            marker in query
            for marker in ("历史", "当前", "实时", "incident", "工单", "部署", "发布")
        )
    if subgoal in {"boundary", "diagnosis"}:
        return False
    return True
