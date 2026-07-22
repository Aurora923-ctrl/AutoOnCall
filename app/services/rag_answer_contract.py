"""Build and validate claim-level contracts for grounded RAG answers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.services.rag_evidence_plan import FrozenGenerationEvidence
from app.services.rag_question_plan import QuestionPlan


@dataclass(frozen=True, slots=True)
class AnswerSlot:
    subgoal_id: str
    required_entities: tuple[str, ...]
    allowed_citation_indices: tuple[int, ...]
    required_source_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerContract:
    slots: tuple[AnswerSlot, ...]
    max_claims: int
    citation_source_roles: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ContractViolation:
    code: str
    subgoal_id: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _AnswerLine:
    text: str
    citation_indices: tuple[int, ...]
    evidence_gap: bool
    specific_gap: bool


_NUMBERED_CITATION = re.compile(r"\[\s*证据\s*(\d+)\s*\]", re.IGNORECASE)
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+]\s*|\d+[.)、]\s*)")
_HISTORICAL_MARKERS = (
    "历史",
    "复盘",
    "工单",
    "部署记录",
    "historical",
    "retrospective",
)
_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "慢查询": ("慢查询", "slow_query", "slow_queries", "slow query", "slow queries"),
    "pool_waiting": ("pool_waiting", "pool waiting", "pool-waiting"),
    "active_connections": (
        "active_connections",
        "active connections",
        "active-connections",
    ),
    "explain": ("explain",),
    "connected_clients": (
        "connected_clients",
        "connected clients",
        "connected-clients",
    ),
    "maxclients": ("maxclients", "max clients", "max_clients"),
    "blocked_clients": ("blocked_clients", "blocked clients", "blocked-clients"),
    "effective_capacity": (
        "effective_capacity",
        "effective capacity",
        "effective-capacity",
        "有效容量",
    ),
    "oomkilled": ("oomkilled", "oom killed", "oom-killed"),
}
_CHANGE_TEMPLATE_MARKERS = (
    "approver",
    "审批人",
    "执行人",
    "canary",
    "灰度",
    "观察时长",
    "observation window",
    "rollback",
    "回滚",
)
_SECTION_TITLES = {
    "已知上下文事实",
    "当前事故仍需查询的证据",
    "允许的处置建议与安全边界",
    "不确定项",
}


def build_answer_contract(
    plan: QuestionPlan,
    frozen_evidence: FrozenGenerationEvidence,
) -> AnswerContract:
    """Bind planned answer slots to the exact frozen evidence citation numbers."""
    citation_indices = _frozen_citation_indices(frozen_evidence)
    slots = tuple(
        AnswerSlot(
            subgoal_id=subgoal.id,
            required_entities=subgoal.required_entities,
            allowed_citation_indices=tuple(
                dict.fromkeys(
                    citation_indices[(binding.source_file, binding.chunk_id)]
                    for binding in frozen_evidence.bindings
                    if binding.subgoal_id == subgoal.id
                    and (binding.source_file, binding.chunk_id) in citation_indices
                )
            ),
            required_source_roles=subgoal.required_source_roles,
        )
        for subgoal in plan.subgoals
    )
    citation_source_roles = tuple(
        dict.fromkeys(
            (
                citation_indices[(binding.source_file, binding.chunk_id)],
                binding.source_role,
            )
            for binding in frozen_evidence.bindings
            if (binding.source_file, binding.chunk_id) in citation_indices
        )
    )
    return AnswerContract(
        slots=slots,
        max_claims=plan.max_claims,
        citation_source_roles=citation_source_roles,
    )


def validate_answer_contract(
    answer: str,
    contract: AnswerContract,
    citations: list[dict[str, Any]],
) -> tuple[ContractViolation, ...]:
    """Return stable violations for claims that do not satisfy the answer contract."""
    lines = _answer_lines(answer)
    issued_indices = _issued_citation_indices(citations)
    allowed_indices = {
        index for slot in contract.slots for index in slot.allowed_citation_indices
    }
    roles_by_index = _roles_by_citation_index(contract)
    violations: list[ContractViolation] = []

    if len(lines) > contract.max_claims:
        violations.append(
            ContractViolation(
                "max_claims_exceeded",
                detail=f"expected<={contract.max_claims};actual={len(lines)}",
            )
        )

    valid_claims: list[_AnswerLine] = []
    specific_gaps: list[_AnswerLine] = []
    for line in lines:
        if line.evidence_gap:
            if line.citation_indices:
                violations.append(
                    ContractViolation("citation_on_evidence_gap", detail=line.text)
                )
            if not line.specific_gap:
                violations.append(
                    ContractViolation("unspecified_evidence_gap", detail=line.text)
                )
            else:
                specific_gaps.append(line)
            continue

        if not re.search(r"[a-z0-9_\u4e00-\u9fff]", line.text, re.IGNORECASE):
            violations.append(ContractViolation("empty_claim"))
            continue
        if not line.citation_indices:
            violations.append(ContractViolation("missing_citation_in_claim", detail=line.text))
            continue
        if len(line.citation_indices) != 1:
            violations.append(
                ContractViolation("multiple_citations_in_claim", detail=line.text)
            )
            continue

        citation_index = line.citation_indices[0]
        if citation_index not in issued_indices:
            violations.append(
                ContractViolation(
                    f"unknown_citation:{citation_index}",
                    detail=line.text,
                )
            )
            continue
        if allowed_indices and citation_index not in allowed_indices:
            violations.append(
                ContractViolation(
                    f"citation_not_allowed:{citation_index}",
                    detail=line.text,
                )
            )
            continue

        valid_claims.append(line)
        cited_roles = roles_by_index.get(citation_index, ())
        if any(role in {"postmortem", "ticket"} for role in cited_roles) and not _contains_any(
            line.text,
            _HISTORICAL_MARKERS,
        ):
            violations.append(
                ContractViolation(
                    "missing_historical_boundary",
                    detail=f"citation_index={citation_index}",
                )
            )

    if not any(slot.subgoal_id == "boundary" for slot in contract.slots):
        change_candidate = "\n".join(
            line.text for line in lines if not line.evidence_gap
        )
        if _is_change_template(change_candidate):
            violations.append(
                ContractViolation(
                    "unrequested_change_template",
                    detail=change_candidate,
                )
            )

    for slot in contract.slots:
        slot_claims = [
            line
            for line in valid_claims
            if line.citation_indices[0] in slot.allowed_citation_indices
        ]
        slot_gaps = [line for line in specific_gaps if _gap_matches_slot(line.text, slot)]
        if not slot_claims and not slot_gaps:
            violations.append(
                ContractViolation(
                    f"missing_subgoal:{slot.subgoal_id}",
                    subgoal_id=slot.subgoal_id,
                )
            )

        searchable = "\n".join(line.text for line in (*slot_claims, *slot_gaps))
        for entity in slot.required_entities:
            if not _contains_entity(searchable, entity):
                violations.append(
                    ContractViolation(
                        f"missing_entity:{entity}",
                        subgoal_id=slot.subgoal_id,
                        detail=entity,
                    )
                )

        for source_role in slot.required_source_roles:
            if not any(
                source_role
                in roles_by_index.get(line.citation_indices[0], ())
                for line in slot_claims
            ):
                violations.append(
                    ContractViolation(
                        f"missing_source_role:{source_role}",
                        subgoal_id=slot.subgoal_id,
                        detail=source_role,
                    )
                )

    return tuple(_deduplicate_violations(violations))


def contract_repair_instructions(
    violations: Iterable[ContractViolation],
    contract: AnswerContract | None = None,
) -> str:
    """Render a bounded repair request containing only contract failure metadata."""
    unique = tuple(_deduplicate_violations(list(violations)))
    if not unique:
        return ""
    slots_by_id = {slot.subgoal_id: slot for slot in contract.slots} if contract else {}
    lines = ["请仅修复以下答案契约违规，不扩写无关内容："]
    for violation in unique:
        parts = [violation.code]
        if violation.subgoal_id:
            parts.append(f"subgoal={violation.subgoal_id}")
            slot = slots_by_id.get(violation.subgoal_id)
            if slot is not None:
                allowed = ",".join(str(index) for index in slot.allowed_citation_indices)
                parts.append(f"allowed_evidence={allowed or 'none'}")
        if violation.detail:
            parts.append(f"detail={violation.detail}")
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def _frozen_citation_indices(
    frozen_evidence: FrozenGenerationEvidence,
) -> dict[tuple[str, str], int]:
    indices: dict[tuple[str, str], int] = {}
    for fallback_index, item in enumerate(frozen_evidence.items, 1):
        try:
            citation_index = int(item.get("citation_index") or fallback_index)
        except (TypeError, ValueError):
            continue
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if citation_index > 0 and source_file and chunk_id:
            indices.setdefault((source_file, chunk_id), citation_index)
    return indices


def _issued_citation_indices(citations: list[dict[str, Any]]) -> set[int]:
    issued: set[int] = set()
    for fallback_index, item in enumerate(citations, 1):
        if not isinstance(item, dict):
            continue
        try:
            citation_index = int(item.get("citation_index") or fallback_index)
        except (TypeError, ValueError):
            continue
        if citation_index > 0:
            issued.add(citation_index)
    return issued


def _roles_by_citation_index(contract: AnswerContract) -> dict[int, tuple[str, ...]]:
    grouped: dict[int, list[str]] = {}
    for citation_index, source_role in contract.citation_source_roles:
        if source_role and source_role not in grouped.setdefault(citation_index, []):
            grouped[citation_index].append(source_role)
    return {index: tuple(roles) for index, roles in grouped.items()}


def _answer_lines(answer: str) -> list[_AnswerLine]:
    content = str(answer or "").split("引用来源：", 1)[0]
    lines: list[_AnswerLine] = []
    for raw_line in content.splitlines():
        text = raw_line.strip()
        if not text:
            continue
        normalized = _BULLET_PREFIX.sub("", text).strip()
        title = normalized.strip("#*:： ").strip()
        if title in _SECTION_TITLES or normalized.startswith("#"):
            continue
        citation_indices = tuple(int(value) for value in _NUMBERED_CITATION.findall(text))
        claim_text = _NUMBERED_CITATION.sub("", normalized).strip()
        evidence_gap = claim_text.startswith("当前证据不足")
        lines.append(
            _AnswerLine(
                text=claim_text,
                citation_indices=citation_indices,
                evidence_gap=evidence_gap,
                specific_gap=evidence_gap and _is_specific_gap(claim_text),
            )
        )
    return lines


def _is_specific_gap(text: str) -> bool:
    detail = re.sub(r"^当前证据不足\s*[:：，,]?\s*", "", text).strip("。.!！?？ ")
    normalized = "".join(detail.casefold().split())
    return bool(normalized) and normalized not in {
        "缺少证据",
        "证据不足",
        "无法判断",
        "信息不足",
        "需要更多证据",
    }


def _contains_entity(text: str, entity: str) -> bool:
    normalized_entity = str(entity or "").strip().casefold()
    if not normalized_entity:
        return True
    aliases = _ENTITY_ALIASES.get(normalized_entity, (normalized_entity,))
    lowered = str(text or "").casefold()
    for alias in aliases:
        normalized_alias = alias.casefold()
        if re.search(r"[a-z0-9_]", normalized_alias):
            if re.search(
                rf"(?<![a-z0-9_]){re.escape(normalized_alias)}(?![a-z0-9_])",
                lowered,
            ):
                return True
        elif normalized_alias in lowered:
            return True
    return False


def _gap_matches_slot(text: str, slot: AnswerSlot) -> bool:
    if any(_contains_entity(text, entity) for entity in slot.required_entities):
        return True
    markers = {
        "evidence": ("指标", "日志", "命令", "证据"),
        "diagnosis": ("原因", "根因", "判断", "诊断"),
        "alert_design": ("告警", "症状", "用户影响"),
        "boundary": ("审批", "回滚", "处置", "变更"),
        "temporal_boundary": ("当前", "历史", "事故窗口", "部署记录", "工单"),
    }
    return _contains_any(text, markers.get(slot.subgoal_id, ()))


def _is_change_template(text: str) -> bool:
    lowered = str(text or "").casefold()
    marker_count = sum(marker.casefold() in lowered for marker in _CHANGE_TEMPLATE_MARKERS)
    return marker_count >= 2 or (
        any(marker in lowered for marker in ("变更计划", "change plan"))
        and marker_count >= 1
    )


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    lowered = str(text or "").casefold()
    return any(str(marker).casefold() in lowered for marker in markers)


def _deduplicate_violations(
    violations: list[ContractViolation],
) -> list[ContractViolation]:
    unique: list[ContractViolation] = []
    seen: set[tuple[str, str]] = set()
    for violation in violations:
        identity = (violation.code, violation.subgoal_id)
        if identity not in seen:
            unique.append(violation)
            seen.add(identity)
    return unique
