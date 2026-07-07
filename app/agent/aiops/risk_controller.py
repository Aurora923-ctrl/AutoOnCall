"""Risk policy checks for AIOps plan steps and remediation actions."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from app.models.approval import RiskAssessment
from app.models.plan import PlanStep
from app.services.incident_lifecycle import is_production_environment
from app.tools.base import RiskLevel

RiskPolicy = Literal["allow", "approval_required", "forbidden"]

RISK_ORDER: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}

READ_ONLY_PREFIXES = ("query_", "search_", "get_", "retrieve_")
READ_ONLY_TOOL_NAMES = {"manual_analysis", "suggest_remediation"}

HARD_FORBIDDEN_TOOLS = {
    "delete_pod",
    "delete_k8s_pod",
    "restart_database",
    "modify_prod_config",
    "execute_shell",
    "run_shell",
    "run_command",
    "execute_command",
    "execute_sql",
    "run_sql",
    "kill_process",
    "shutdown_host",
}

ACTION_TOOLS_REQUIRING_APPROVAL = {
    "restart_service",
    "scale_service",
    "rollback_deployment",
    "apply_config_change",
    "drain_node",
    "clear_cache",
}

FORBIDDEN_PATTERNS = [
    (re.compile(r"\brm\s+-rf\b", re.IGNORECASE), "shell:rm-rf"),
    (re.compile(r"\bkill\s+-9\b", re.IGNORECASE), "shell:kill-9"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "shell:shutdown"),
    (re.compile(r"\bkubectl\s+delete\b", re.IGNORECASE), "k8s:delete"),
    (re.compile(r"\bdrop\s+table\b", re.IGNORECASE), "sql:drop-table"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "sql:truncate-table"),
    (re.compile(r"\bdelete\s+from\b", re.IGNORECASE), "sql:delete"),
    (re.compile(r"\bupdate\s+\w+", re.IGNORECASE), "sql:update"),
    (re.compile(r"\binsert\s+into\b", re.IGNORECASE), "sql:insert"),
    (re.compile(r"\balter\s+table\b", re.IGNORECASE), "sql:alter-table"),
    (re.compile(r"删除\s*pod", re.IGNORECASE), "k8s:delete-pod"),
    (re.compile(r"重启.*数据库", re.IGNORECASE), "database:restart"),
    (re.compile(r"修改.*生产.*配置", re.IGNORECASE), "config:modify-prod"),
    (re.compile(r"未审核.*sql", re.IGNORECASE), "sql:unaudited"),
]

APPROVAL_PATTERNS = [
    (
        re.compile(r"\brestart\s+(service|pod|database|db|deployment)\b", re.IGNORECASE),
        "action:restart",
    ),
    (re.compile(r"\brollback\s+(service|deployment|release)\b", re.IGNORECASE), "action:rollback"),
    (
        re.compile(r"\bscale\s+(service|deployment|replica|replicas)\b", re.IGNORECASE),
        "action:scale",
    ),
    (re.compile(r"\blimit\s+traffic\b", re.IGNORECASE), "action:limit-traffic"),
    (re.compile(r"重启.*服务", re.IGNORECASE), "action:restart-service"),
    (re.compile(r"扩容|缩容", re.IGNORECASE), "action:scale"),
    (re.compile(r"回滚", re.IGNORECASE), "action:rollback"),
    (re.compile(r"限流|降级", re.IGNORECASE), "action:traffic-control"),
    (re.compile(r"调整.*maxclients", re.IGNORECASE), "action:redis-config"),
    (re.compile(r"修改.*配置", re.IGNORECASE), "action:config-change"),
]


class RiskControlDecision(BaseModel):
    """Result of applying risk policy to a planned action."""

    action: str
    tool_name: str = ""
    step_id: str | None = None
    risk_level: RiskLevel = "low"
    read_only: bool = True
    policy: RiskPolicy = "allow"
    need_approval: bool = False
    allowed: bool = True
    forbidden: bool = False
    reason: str = ""
    matched_rules: list[str] = Field(default_factory=list)

    def to_risk_assessment(self) -> RiskAssessment:
        """Convert the policy decision into the public risk model."""
        return RiskAssessment(
            action=self.action,
            risk_level=self.risk_level,
            reason=self.reason,
            need_approval=self.need_approval,
            policy=self.policy,
            allowed=self.allowed,
            forbidden=self.forbidden,
            matched_rules=self.matched_rules,
        )


def assess_plan_step(
    step: PlanStep | dict[str, Any],
    tool_registry: Any | None = None,
    incident: Any | None = None,
) -> RiskControlDecision:
    """Assess whether a plan step can be executed automatically."""
    plan_step = step if isinstance(step, PlanStep) else PlanStep(**step)
    metadata = _get_tool_metadata(plan_step.tool_name, tool_registry)
    read_only = _resolve_read_only(plan_step, metadata)
    risk_level = _max_risk(plan_step.risk_level, _metadata_risk(metadata))
    context = _step_context(plan_step, incident)

    forbidden_rules = _matched_forbidden_rules(plan_step, context)
    if forbidden_rules:
        return RiskControlDecision(
            action=_action_text(plan_step),
            tool_name=plan_step.tool_name,
            step_id=plan_step.step_id,
            risk_level="high",
            read_only=read_only,
            policy="forbidden",
            need_approval=True,
            allowed=False,
            forbidden=True,
            reason=f"命中禁止自动执行规则: {', '.join(forbidden_rules)}",
            matched_rules=forbidden_rules,
        )

    approval_rules = _matched_approval_rules(plan_step, context, read_only, risk_level)
    if approval_rules:
        approval_risk = _risk_for_approval(plan_step, incident, read_only, risk_level)
        return RiskControlDecision(
            action=_action_text(plan_step),
            tool_name=plan_step.tool_name,
            step_id=plan_step.step_id,
            risk_level=approval_risk,
            read_only=read_only,
            policy="approval_required",
            need_approval=True,
            allowed=False,
            forbidden=False,
            reason=f"动作需要人工审批: {', '.join(approval_rules)}",
            matched_rules=approval_rules,
        )

    return RiskControlDecision(
        action=_action_text(plan_step),
        tool_name=plan_step.tool_name,
        step_id=plan_step.step_id,
        risk_level=risk_level,
        read_only=read_only,
        policy="allow",
        need_approval=False,
        allowed=True,
        forbidden=False,
        reason=_allow_reason(plan_step, read_only, risk_level),
    )


def is_auto_executable(
    step: PlanStep | dict[str, Any],
    tool_registry: Any | None = None,
    incident: Any | None = None,
) -> bool:
    """Return True when a step can run without human approval."""
    return assess_plan_step(step, tool_registry, incident).policy == "allow"


def _get_tool_metadata(tool_name: str, tool_registry: Any | None) -> Any | None:
    if not tool_registry or not hasattr(tool_registry, "get"):
        return None
    return tool_registry.get(tool_name)


def _resolve_read_only(step: PlanStep, metadata: Any | None) -> bool:
    if metadata is not None and hasattr(metadata, "read_only"):
        return bool(metadata.read_only)
    if step.tool_name in HARD_FORBIDDEN_TOOLS or step.tool_name in ACTION_TOOLS_REQUIRING_APPROVAL:
        return False
    return step.tool_name.startswith(READ_ONLY_PREFIXES) or step.tool_name in READ_ONLY_TOOL_NAMES


def _metadata_risk(metadata: Any | None) -> RiskLevel:
    value = getattr(metadata, "risk_level", "low") if metadata is not None else "low"
    return cast(RiskLevel, value) if value in RISK_ORDER else "low"


def _max_risk(*levels: str) -> RiskLevel:
    normalized: list[RiskLevel] = []
    for level in levels:
        if level in RISK_ORDER:
            normalized.append(cast(RiskLevel, level))
    if not normalized:
        return "low"
    return max(normalized, key=lambda level: RISK_ORDER[level])


def _step_context(step: PlanStep, incident: Any | None) -> str:
    parts = [
        step.tool_name,
        step.purpose,
        step.expected_evidence,
        _json_text(step.input_args),
        _incident_context(incident),
    ]
    return " ".join(part for part in parts if part).lower()


def _incident_context(incident: Any | None) -> str:
    if not incident:
        return ""
    if isinstance(incident, dict):
        values = [
            incident.get("title", ""),
            incident.get("service_name", ""),
            incident.get("severity", ""),
            incident.get("symptom", ""),
            incident.get("environment", ""),
        ]
    else:
        values = [
            getattr(incident, "title", ""),
            getattr(incident, "service_name", ""),
            getattr(incident, "severity", ""),
            getattr(incident, "symptom", ""),
            getattr(incident, "environment", ""),
        ]
    return " ".join(str(value) for value in values if value)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _matched_forbidden_rules(step: PlanStep, context: str) -> list[str]:
    rules: list[str] = []
    if step.tool_name in HARD_FORBIDDEN_TOOLS:
        rules.append(f"tool:{step.tool_name}")
    if _has_unaudited_sql(step):
        rules.append("sql:unaudited")
    for pattern, rule_name in FORBIDDEN_PATTERNS:
        if pattern.search(context):
            rules.append(rule_name)
    return _dedupe(rules)


def _has_unaudited_sql(step: PlanStep) -> bool:
    tool_name = step.tool_name.lower()
    if "sql" not in tool_name and "sql" not in step.input_args:
        return False
    if step.input_args.get("audited") is True:
        return False
    sql = str(step.input_args.get("sql") or step.input_args.get("query") or "")
    if not sql and tool_name in {"execute_sql", "run_sql"}:
        return True
    return bool(
        re.search(
            r"\b(drop|truncate|delete|update|insert|alter|create|grant|revoke)\b",
            sql,
            flags=re.IGNORECASE,
        )
    )


def _matched_approval_rules(
    step: PlanStep,
    context: str,
    read_only: bool,
    risk_level: RiskLevel,
) -> list[str]:
    rules: list[str] = []
    if read_only and step.tool_name == "suggest_remediation":
        return _dedupe(rules)
    if risk_level == "high" or (risk_level == "medium" and not read_only):
        rules.append(f"risk:{risk_level}")
    if not read_only:
        rules.append("tool:not-read-only")
    if step.tool_name in ACTION_TOOLS_REQUIRING_APPROVAL:
        rules.append(f"tool:{step.tool_name}")
    for pattern, rule_name in APPROVAL_PATTERNS:
        if pattern.search(context):
            rules.append(rule_name)
    return _dedupe(rules)


def _risk_for_approval(
    step: PlanStep,
    incident: Any | None,
    read_only: bool,
    risk_level: RiskLevel,
) -> RiskLevel:
    if risk_level == "high":
        return "high"
    if not read_only and _is_prod_incident(incident):
        return "high"
    if step.tool_name in ACTION_TOOLS_REQUIRING_APPROVAL and _is_prod_incident(incident):
        return "high"
    return _max_risk(risk_level, "medium")


def _is_prod_incident(incident: Any | None) -> bool:
    if not incident:
        return False
    environment = (
        incident.get("environment", "")
        if isinstance(incident, dict)
        else getattr(incident, "environment", "")
    )
    return is_production_environment(environment)


def _action_text(step: PlanStep) -> str:
    return step.purpose or step.tool_name or "未命名动作"


def _allow_reason(step: PlanStep, read_only: bool, risk_level: RiskLevel) -> str:
    if read_only and step.tool_name == "suggest_remediation":
        return "只读建议动作，仅生成处置建议，不创建审批单"
    if risk_level == "low":
        return "只读低风险动作，允许自动执行"
    return "只读动作，允许自动执行"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
