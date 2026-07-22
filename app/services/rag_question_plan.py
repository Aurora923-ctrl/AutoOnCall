"""Explicit, immutable planning for grounded RAG questions."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnswerSubgoal:
    id: str
    intent: str
    required_entities: tuple[str, ...]
    required_source_roles: tuple[str, ...]
    action_requested: bool = False
    temporal_boundary_required: bool = False


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    query: str
    domain: str
    explicit_entities: tuple[str, ...]
    subgoals: tuple[AnswerSubgoal, ...]
    max_claims: int


_DOMAIN_DEFINITIONS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "mysql",
        ("mysql", "慢查询", "pool_waiting", "active_connections"),
        ("pool_waiting", "active_connections", "慢查询"),
    ),
    (
        "redis",
        ("redis", "connected_clients", "maxclients", "blocked_clients"),
        ("connected_clients", "maxclients", "blocked_clients"),
    ),
    ("memory", ("oomkilled", "oom", "内存", "rss", "working_set"), ("OOMKilled",)),
    ("kubernetes", ("kubernetes", "pod", "endpointslice"), ("Pod",)),
    ("loki", ("loki", "日志"), ()),
    ("dns", ("dns", "nxdomain", "servfail", "解析"), ()),
)

_ACTION_MARKERS = (
    "重启",
    "扩容",
    "限流",
    "回滚",
    "删除",
    "清理",
    "截断",
    "修改",
    "调整",
    "执行",
    "变更",
)
_EVIDENCE_MARKERS = ("证据", "取证", "检查", "排查", "验证", "指标", "日志", "命令")
_DIAGNOSIS_MARKERS = (
    "区分",
    "判断",
    "原因",
    "定位",
    "根因",
    "为什么",
    "如何判断",
    "如何排查",
    "事故复盘",
    "部署历史",
)
_TEMPORAL_MARKERS = ("历史", "当前", "实时", "事故窗口", "incident-window", "复盘", "工单", "部署", "发布")
_SERVICE_PATTERN = re.compile(r"\b[\w-]+-service\b", re.IGNORECASE)


def build_question_plan(query: str) -> QuestionPlan:
    """Build the required evidence and decision subgoals from a user question."""
    raw_query = str(query or "").strip()
    normalized_query = raw_query.casefold()
    domain, domain_entities = _identify_domain(normalized_query)
    entities = list(_service_entities(raw_query))
    entities.extend(entity for entity in domain_entities if entity.casefold() in normalized_query)

    if domain == "redis" and _is_redis_capacity_question(normalized_query):
        _append_unique(entities, "effective_capacity")
        _append_unique(entities, "blocked_clients")
    if domain == "mysql" and _asks_how_to_investigate_slow_query(normalized_query):
        _append_unique(entities, "EXPLAIN")

    action_requested = any(marker in raw_query for marker in _ACTION_MARKERS)
    temporal_boundary_required = any(marker in normalized_query for marker in _TEMPORAL_MARKERS)
    has_evidence = any(marker in normalized_query for marker in _EVIDENCE_MARKERS)
    has_diagnosis = any(marker in normalized_query for marker in _DIAGNOSIS_MARKERS)

    subgoals: list[AnswerSubgoal] = []
    if has_evidence:
        subgoals.append(
            AnswerSubgoal("evidence", "evidence", tuple(entities), _source_roles(normalized_query))
        )
    if has_diagnosis:
        subgoals.append(
            AnswerSubgoal("diagnosis", "diagnosis", tuple(entities), _source_roles(normalized_query))
        )
    if any(marker in normalized_query for marker in ("告警", "症状告警", "设计告警", "用户可见")):
        subgoals.append(AnswerSubgoal("alert_design", "alert_design", tuple(entities), ()))
    if action_requested:
        subgoals.append(
            AnswerSubgoal(
                "boundary",
                "action",
                tuple(entities),
                (),
                action_requested=True,
            )
        )
    if temporal_boundary_required:
        subgoals.append(
            AnswerSubgoal(
                "temporal_boundary",
                "temporal_boundary",
                tuple(entities),
                (),
                temporal_boundary_required=True,
            )
        )

    if not subgoals:
        subgoals.append(AnswerSubgoal("evidence", "evidence", tuple(entities), ()))

    max_claims = 5 if domain == "redis" and _is_redis_capacity_question(normalized_query) else 3
    return QuestionPlan(raw_query, domain, tuple(entities), tuple(subgoals), max_claims)


def entities_for_subgoal(plan: QuestionPlan, subgoal_id: str) -> tuple[str, ...]:
    """Return entity requirements for a planned subgoal, if it exists."""
    for subgoal in plan.subgoals:
        if subgoal.id == subgoal_id:
            return subgoal.required_entities
    return ()


def _identify_domain(normalized_query: str) -> tuple[str, tuple[str, ...]]:
    for domain, markers, entities in _DOMAIN_DEFINITIONS:
        if any(marker in normalized_query for marker in markers):
            return domain, entities
    return "general", ()


def _service_entities(query: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _SERVICE_PATTERN.finditer(query))


def _is_redis_capacity_question(normalized_query: str) -> bool:
    return "redis" in normalized_query and (
        "connected_clients" in normalized_query or "maxclients" in normalized_query
    )


def _asks_how_to_investigate_slow_query(normalized_query: str) -> bool:
    return "慢查询" in normalized_query and any(
        marker in normalized_query
        for marker in ("如何排查", "怎么排查", "怎样排查", "如何调查", "如何定位", "怎么查", "如何检查")
    )


def _source_roles(normalized_query: str) -> tuple[str, ...]:
    roles: list[str] = []
    if any(marker in normalized_query for marker in ("官方", "官方限制", "官方文档")):
        roles.append("official")
    if any(marker in normalized_query for marker in ("事故复盘", "复盘", "postmortem")):
        roles.append("postmortem")
    return tuple(roles)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
