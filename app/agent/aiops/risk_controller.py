"""Risk policy checks for AIOps plan steps and remediation actions."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, cast

from pydantic import BaseModel, Field

from app.models.approval import RiskAssessment
from app.models.plan import PlanStep
from app.services.incident_lifecycle import is_production_environment
from app.services.policies.approval_policy import (
    ACTION_TOOLS_REQUIRING_APPROVAL,
    HARD_FORBIDDEN_TOOLS,
    READ_ONLY_TOOL_NAMES,
    RISK_POLICY_VERSION,
    RiskPolicy,
    matched_action_patterns,
)
from app.tools.base import RiskLevel

RISK_ORDER: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}


class RiskControlDecision(BaseModel):
    """Result of applying risk policy to a planned action."""

    action: str
    tool_name: str = ""
    step_id: str | None = None
    input_args: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = "low"
    read_only: bool = True
    policy: RiskPolicy = "allow"
    need_approval: bool = False
    allowed: bool = True
    forbidden: bool = False
    reason: str = ""
    matched_rules: list[str] = Field(default_factory=list)
    policy_version: str = RISK_POLICY_VERSION

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
    risk_level = _max_risk(
        plan_step.risk_level,
        _metadata_risk(metadata, registry_present=tool_registry is not None),
    )
    context = _step_context(plan_step)

    forbidden_rules = _matched_forbidden_rules(plan_step, context)
    if forbidden_rules:
        return RiskControlDecision(
            action=_action_text(plan_step),
            tool_name=plan_step.tool_name,
            step_id=plan_step.step_id,
            input_args=plan_step.input_args,
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
            input_args=plan_step.input_args,
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
        input_args=plan_step.input_args,
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
    if not tool_registry:
        return None
    if hasattr(tool_registry, "get_policy_metadata"):
        return tool_registry.get_policy_metadata(tool_name)
    if not hasattr(tool_registry, "get"):
        return None
    return tool_registry.get(tool_name)


def _resolve_read_only(step: PlanStep, metadata: Any | None) -> bool:
    if isinstance(metadata, dict) and "read_only" in metadata:
        return bool(metadata["read_only"])
    if metadata is not None and hasattr(metadata, "read_only"):
        return bool(metadata.read_only)
    tool_name = _normalize_tool_name(step.tool_name)
    if tool_name in HARD_FORBIDDEN_TOOLS or tool_name in ACTION_TOOLS_REQUIRING_APPROVAL:
        return False
    return tool_name in READ_ONLY_TOOL_NAMES


def _metadata_risk(metadata: Any | None, *, registry_present: bool = False) -> RiskLevel:
    if metadata is None and registry_present:
        return "high"
    value = (
        metadata.get("risk_level", "low")
        if isinstance(metadata, dict)
        else getattr(metadata, "risk_level", "low")
        if metadata is not None
        else "low"
    )
    return cast(RiskLevel, value) if value in RISK_ORDER else "low"


def _max_risk(*levels: str) -> RiskLevel:
    normalized: list[RiskLevel] = []
    for level in levels:
        if level in RISK_ORDER:
            normalized.append(cast(RiskLevel, level))
    if not normalized:
        return "low"
    return max(normalized, key=lambda level: RISK_ORDER[level])


def _step_context(step: PlanStep) -> str:
    """Return only action-owned text used for action-pattern matching."""
    parts = [step.tool_name, step.purpose, step.expected_evidence, *_structured_text(step.input_args)]
    return _normalize_action_text(" ".join(part for part in parts if part))


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _structured_text(value: Any) -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for key, item in value.items():
            items.append(str(key))
            items.extend(_structured_text(item))
        return items
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            items.extend(_structured_text(item))
        return items
    return [str(value)]


def _normalize_action_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"(?m)(?:--|#)[^\r\n]*", " ", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return re.sub(r"\s+", " ", text).strip()


def _matched_forbidden_rules(step: PlanStep, context: str) -> list[str]:
    rules: list[str] = []
    tool_name = _normalize_tool_name(step.tool_name)
    if tool_name in HARD_FORBIDDEN_TOOLS:
        rules.append(f"tool:{tool_name}")
    if _has_unaudited_sql(step):
        rules.append("sql:unaudited")
    rules.extend(matched_action_patterns(context, forbidden=True))
    return _dedupe(rules)


def _has_unaudited_sql(step: PlanStep) -> bool:
    tool_name = _normalize_tool_name(step.tool_name)
    if "sql" not in tool_name and "sql" not in step.input_args:
        return False
    sql = _normalize_action_text(
        str(step.input_args.get("sql") or step.input_args.get("query") or "")
    )
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
    tool_name = _normalize_tool_name(step.tool_name)
    if tool_name == "suggest_remediation" and read_only:
        return []
    if read_only and tool_name != "manual_analysis":
        return []
    rules: list[str] = []
    if risk_level == "high" or (risk_level == "medium" and not read_only):
        rules.append(f"risk:{risk_level}")
    if not read_only:
        rules.append("tool:not-read-only")
    if tool_name in ACTION_TOOLS_REQUIRING_APPROVAL:
        rules.append(f"tool:{tool_name}")
    rules.extend(matched_action_patterns(context, forbidden=False))
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
    if _normalize_tool_name(step.tool_name) in ACTION_TOOLS_REQUIRING_APPROVAL and _is_prod_incident(
        incident
    ):
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


def _normalize_tool_name(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
