"""Answer-policy helpers for grounded RAG responses."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.rag_answer_contract import AnswerContract, AnswerSlot
from app.services.rag_generation_context import (
    build_generation_context,
    build_generation_evidence,
    citation_source_basename,
    normalize_generation_citation_labels,
    select_generation_excerpt,
)
from app.services.rag_read_models import format_score
from app.services.rag_retrieval.candidates import extract_retrieval_terms
from app.services.rag_retrieval.service import NO_TRUSTED_KNOWLEDGE

__all__ = [
    "build_generation_context",
    "build_generation_evidence",
    "citation_source_basename",
    "normalize_generation_citation_labels",
    "select_generation_excerpt",
]

LEGACY_ANSWER_LABELS = (
    "已知上下文事实",
    "当前事故仍需查询的证据",
    "允许的处置建议与安全边界",
    "不确定项",
)


def build_grounded_question(question: str, retrieval_payload: dict[str, Any]) -> str:
    """Build the final LLM prompt from trusted retrieval context."""
    context = build_generation_context(retrieval_payload)
    contract_instruction = _build_answer_contract_instruction(retrieval_payload)
    return (
        "冻结证据（唯一允许引用范围）:\n"
        f"{context}\n\n"
        f"用户问题: {question}\n\n"
        f"{contract_instruction}\n"
        "只输出契约内的要点，不输出前言、总结、栏目标题或单独来源列表。"
    )


def _build_answer_contract_instruction(retrieval_payload: dict[str, Any]) -> str:
    """Render the exact immutable answer slots exposed by generation preparation."""
    contract = retrieval_payload.get("_answer_contract")
    if not isinstance(contract, AnswerContract):
        return "答案契约:\n- max_claims=3"

    lines = ["答案契约:", f"- max_claims={contract.max_claims}"]
    for slot in contract.slots:
        entities = ",".join(slot.required_entities) or "none"
        allowed = ",".join(str(index) for index in slot.allowed_citation_indices) or "none"
        roles = ",".join(slot.required_source_roles) or "none"
        lines.append(
            f"- slot={slot.subgoal_id}; required_entities={entities}; "
            f"allowed_evidence={allowed}; required_source_roles={roles}"
        )
    if not any(slot.subgoal_id == "boundary" for slot in contract.slots):
        lines.append("- action_slot=absent")
    lines.append(
        "- 每条事实只绑定一个该 slot 允许的 [证据 N]；无允许证据时仅输出"
        "“当前证据不足：缺少 <entity/subgoal>”且不加引用"
    )
    return "\n".join(lines)


def build_grounded_system_prompt() -> str:
    """Return the strict system prompt used for tool-free grounded generation."""
    return (
        "你是知识库问答助手，只能使用用户消息中的冻结证据，不能调用工具或补充外部事实。"
        "每条事实必须紧密归纳一个证据片段，并在行末仅绑定一个 [证据 N]。"
        "静态 Runbook、阈值和示例不得写成当前事故观测；历史复盘或工单必须标明历史，"
        "且不能替代当前 incident-window 证据。"
        "按用户消息中的答案契约逐 slot 输出短要点；无支持证据的 slot 只写具体证据缺口。"
        "不要输出前言、总结、固定栏目或单独来源列表。"
    )


def compress_grounded_answer(answer: str) -> str:
    """Remove legacy scaffolding without rewriting evidence-bearing claims."""
    compact_lines: list[str] = []
    for raw_line in str(answer or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        for label in LEGACY_ANSWER_LABELS:
            line = re.sub(rf"^{re.escape(label)}\s*[:：]\s*", "", line).strip()
        if not line or line in LEGACY_ANSWER_LABELS:
            continue
        if line.startswith(("引用来源", "参考来源")):
            break
        line = re.sub(r"\s+", " ", line)
        compact_lines.append(f"- {line}")
    return "\n".join(compact_lines).strip()


def select_supporting_citations(
    answer: str,
    citations: list[dict[str, Any]],
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep exact allowlisted source/chunk pairs and reject any fabricated pair."""
    allowed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if source_file and chunk_id:
            allowed.setdefault((source_file, chunk_id), item)

    citation_map = citation_pair_map(citations)
    cited_pairs = extract_citation_pairs(answer, citation_map=citation_map)
    if not cited_pairs or any(pair not in allowed for pair in cited_pairs):
        return []
    if not answer_claims_are_cited(
        answer,
        allowed_pairs=set(allowed),
        citation_map=citation_map,
    ):
        return []
    if evidence is not None and not answer_claims_match_evidence(answer, citations, evidence):
        return []

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pair in cited_pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(allowed[pair])
    return unique


def build_extractive_grounded_answer(
    query: str,
    evidence: list[dict[str, Any]],
    *,
    required_sources: list[str] | set[str] | tuple[str, ...] | None = None,
    max_claims: int = 3,
    answer_contract: AnswerContract | None = None,
) -> str:
    """Build a citation-complete fallback from verbatim evidence sentences."""
    normalized_evidence = [item for item in evidence if isinstance(item, dict)]
    if not normalized_evidence:
        return ""
    if answer_contract is not None:
        return _build_slot_aware_extractive_answer(
            query,
            normalized_evidence,
            answer_contract,
        )
    query_terms = _claim_terms(query)
    required = {
        citation_source_basename(source)
        for source in required_sources or ()
        if str(source or "").strip()
    }
    ranked: list[tuple[int, int, dict[str, Any], str]] = []
    for position, item in enumerate(normalized_evidence):
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        if not content:
            continue
        source = citation_source_basename(item.get("source_file"))
        for sentence in _evidence_sentences(content):
            sentence_terms = _claim_terms(sentence)
            overlap = len(query_terms.intersection(sentence_terms))
            source_bonus = 4 if source in required else 0
            ranked.append((source_bonus + overlap, -position, item, sentence))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

    selected: list[tuple[dict[str, Any], str]] = []
    selected_sources: set[str] = set()
    selected_pairs: set[tuple[str, str]] = set()
    for required_source in sorted(required):
        match = next(
            (
                (item, sentence)
                for _score, _position, item, sentence in ranked
                if citation_source_basename(item.get("source_file")) == required_source
            ),
            None,
        )
        if match is None:
            continue
        pair = (
            citation_source_basename(match[0].get("source_file")),
            str(match[0].get("chunk_id") or ""),
        )
        selected.append(match)
        selected_sources.add(required_source)
        selected_pairs.add(pair)
    for _score, _position, item, sentence in ranked:
        if len(selected) >= max_claims:
            break
        pair = (
            citation_source_basename(item.get("source_file")),
            str(item.get("chunk_id") or ""),
        )
        if pair in selected_pairs:
            continue
        selected.append((item, sentence))
        selected_sources.add(pair[0])
        selected_pairs.add(pair)

    lines = []
    for item, sentence in selected[:max_claims]:
        try:
            citation_index = int(item.get("citation_index") or 0)
        except (TypeError, ValueError):
            return ""
        if citation_index <= 0:
            return ""
        lines.append(f"- {sentence} [证据 {citation_index}]")
    return "\n".join(lines)


def _build_slot_aware_extractive_answer(
    query: str,
    evidence: list[dict[str, Any]],
    contract: AnswerContract,
) -> str:
    """Cover each answer slot and required source role from frozen evidence only."""
    query_terms = _claim_terms(query)
    roles_by_index: dict[int, set[str]] = {}
    for citation_index, source_role in contract.citation_source_roles:
        roles_by_index.setdefault(citation_index, set()).add(source_role)

    candidates: list[tuple[int, dict[str, Any], str]] = []
    for item in evidence:
        citation_index = _positive_citation_index(item)
        if citation_index <= 0:
            continue
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        for sentence in _evidence_sentences(content):
            candidates.append((citation_index, item, sentence))

    selected: list[tuple[int, dict[str, Any], str]] = []
    selected_keys: set[tuple[int, str]] = set()
    gaps: list[str] = []
    for slot in contract.slots:
        candidate = _best_slot_sentence(slot, candidates, query_terms)
        if candidate is None:
            target = ",".join(slot.required_entities) or slot.subgoal_id
            gaps.append(f"- 当前证据不足：缺少 {target}")
            continue
        key = (candidate[0], candidate[2])
        if key not in selected_keys:
            selected.append(candidate)
            selected_keys.add(key)

    for slot in contract.slots:
        for source_role in slot.required_source_roles:
            if any(
                citation_index in slot.allowed_citation_indices
                and source_role in roles_by_index.get(citation_index, set())
                for citation_index, _item, _sentence in selected
            ):
                continue
            role_candidates = [
                candidate
                for candidate in candidates
                if candidate[0] in slot.allowed_citation_indices
                and source_role in roles_by_index.get(candidate[0], set())
            ]
            candidate = _best_slot_sentence(slot, role_candidates, query_terms)
            if candidate is None:
                continue
            key = (candidate[0], candidate[2])
            if key not in selected_keys:
                selected.append(candidate)
                selected_keys.add(key)

    lines: list[str] = []
    for citation_index, _item, sentence in selected:
        roles = roles_by_index.get(citation_index, set())
        if "postmortem" in roles and not any(
            marker in sentence.casefold()
            for marker in ("历史", "复盘", "historical", "retrospective")
        ):
            sentence = f"历史复盘：{sentence}"
        elif "ticket" in roles and not any(
            marker in sentence.casefold() for marker in ("历史", "工单", "ticket")
        ):
            sentence = f"历史工单：{sentence}"
        lines.append(f"- {sentence} [证据 {citation_index}]")
    lines.extend(gaps)
    return "\n".join(lines[: contract.max_claims])


def _best_slot_sentence(
    slot: AnswerSlot,
    candidates: list[tuple[int, dict[str, Any], str]],
    query_terms: set[str],
) -> tuple[int, dict[str, Any], str] | None:
    allowed = [
        candidate for candidate in candidates if candidate[0] in slot.allowed_citation_indices
    ]
    if not allowed:
        return None
    return max(
        allowed,
        key=lambda candidate: (
            sum(
                str(entity).casefold() in candidate[2].casefold()
                for entity in slot.required_entities
            ),
            len(query_terms.intersection(_claim_terms(candidate[2]))),
            -candidate[0],
        ),
    )


def _positive_citation_index(item: dict[str, Any]) -> int:
    try:
        citation_index = int(item.get("citation_index") or 0)
    except (TypeError, ValueError):
        return 0
    return citation_index if citation_index > 0 else 0


def _evidence_sentences(content: str) -> list[str]:
    """Return compact complete sentences, excluding headings and code fences."""
    sentences: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip().lstrip("-*# ").strip()
        if not line or line.startswith("```") or len(line) < 8:
            continue
        for sentence in re.split(r"(?<=[。！？.!?])\s+|(?<=。)", line):
            sentence = sentence.strip()
            if 8 <= len(sentence) <= 220:
                sentences.append(sentence)
    return sentences


def answer_claims_match_evidence(
    answer: str,
    citations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> bool:
    """Require each cited claim to share concrete terms with its cited chunk."""
    evidence_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source_file = str(item.get("source_file") or "").strip().replace("\\", "/")
        chunk_id = str(item.get("chunk_id") or "").strip()
        if not source_file or not chunk_id:
            continue
        evidence_by_pair[(source_file, chunk_id)] = item
        evidence_by_pair.setdefault((source_file.rsplit("/", 1)[-1], chunk_id), item)
    citation_map = citation_pair_map(citations)
    for line in (line.strip() for line in str(answer or "").splitlines() if line.strip()):
        pairs = extract_citation_pairs(line, citation_map=citation_map)
        if not pairs:
            continue
        claim = re.sub(r"\[[^\[\]\r\n]+\]", "", line).strip(" -\t")
        claim_terms = _claim_terms(claim)
        if not claim_terms:
            return False
        for pair in pairs:
            chunk = evidence_by_pair.get(pair)
            if chunk is None:
                return False
            chunk_terms = _claim_terms(
                " ".join(
                    (
                        str(chunk.get("source_file") or ""),
                        str(chunk.get("heading_path") or ""),
                        str(chunk.get("content") or chunk.get("content_preview") or ""),
                    )
                )
            )
            if not claim_terms.intersection(chunk_terms):
                return False
    return True


def _claim_terms(text: str) -> set[str]:
    """Extract concrete identifiers and CJK n-grams, excluding boilerplate."""
    stop_terms = {
        "检查",
        "确认",
        "判断",
        "建议",
        "当前",
        "需要",
        "可以",
        "是否",
        "如果",
        "然后",
        "应该",
        "用户",
        "问题",
        "证据",
        "结论",
    }
    return {
        term
        for term in extract_retrieval_terms(text)
        if term not in stop_terms and len(term) > 1
    }


def remove_generic_uncertainty_boilerplate(answer: str) -> str:
    """Drop the unsupported catch-all gap sentence before citation validation."""
    kept_lines = []
    for line in str(answer or "").splitlines():
        normalized = re.sub(r"^\s*(?:[-*]|\d+[.)])?\s*", "", line).strip()
        normalized_gap = re.sub(r"^不确定项\s*[:：]\s*", "", normalized).strip()
        if not extract_citation_pairs(line) and normalized_gap.startswith("当前片段未提供"):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip()


def answer_claims_are_cited(
    answer: str,
    *,
    allowed_pairs: set[tuple[str, str]],
    citation_map: dict[int, tuple[str, str]] | None = None,
) -> bool:
    """Require every substantive answer line to bind only allowlisted chunks."""
    text = str(answer or "")
    content = text.split("引用来源：", 1)[0]
    section_titles = {
        "已知上下文事实",
        "当前事故仍需查询的证据",
        "允许的处置建议与安全边界",
        "不确定项",
    }
    claim_lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized_title = line.strip("#*-:： ").strip()
        if normalized_title in section_titles:
            continue
        claim_lines.append(line)
    if not claim_lines:
        return False
    for line in claim_lines:
        pairs = extract_citation_pairs(line, citation_map=citation_map)
        normalized = re.sub(r"^\s*(?:[-*]|\d+[.)])?\s*", "", line).strip()
        if normalized.startswith("当前证据不足"):
            continue
        if not pairs or any(pair not in allowed_pairs for pair in pairs):
            return False
    return True


def validated_citation_prefix(
    answer: str,
    citations: list[dict[str, Any]],
) -> str:
    """Return the longest complete prefix whose citation references are allowlisted."""
    text = str(answer or "")
    boundary = 0
    for match in re.finditer(r"\[[^\[\]\r\n]+\]", text):
        candidate = text[: match.end()]
        if _prefix_has_allowlisted_citations(candidate, citations):
            boundary = match.end()
    return text[:boundary]


def _prefix_has_allowlisted_citations(
    text: str,
    citations: list[dict[str, Any]],
) -> bool:
    """Validate complete citation-bearing lines without requiring final answer shape."""
    allowed = {
        (
            str(item.get("source_file") or "").strip(),
            str(item.get("chunk_id") or "").strip(),
        )
        for item in citations
        if isinstance(item, dict)
    }
    citation_map = citation_pair_map(citations)
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines or not allowed:
        return False
    for line in lines:
        pairs = extract_citation_pairs(line, citation_map=citation_map)
        if pairs and len(pairs) == 1 and pairs[0] in allowed:
            continue
        if line.endswith(("。", ".", "!", "！", "?", "？", ":", "：")):
            return False
    return True


def citation_pair_map(citations: list[dict[str, Any]]) -> dict[int, tuple[str, str]]:
    """Map stable server-issued evidence numbers to exact source/chunk identities."""
    mapping: dict[int, tuple[str, str]] = {}
    for fallback_index, item in enumerate(citations, 1):
        try:
            citation_index = int(item.get("citation_index") or fallback_index)
        except (TypeError, ValueError):
            continue
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if citation_index > 0 and source_file and chunk_id and citation_index not in mapping:
            mapping[citation_index] = (source_file, chunk_id)
    return mapping


def extract_citation_pairs(
    answer: str,
    *,
    citation_map: dict[int, tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Parse allowlist-bound citation references from a grounded answer."""
    pairs: list[tuple[str, str]] = []
    for raw_reference in re.findall(r"\[([^\[\]\r\n]+)\]", str(answer or "")):
        numbered = re.fullmatch(r"\s*(?:证据\s*)?(\d+)\s*", raw_reference)
        if numbered:
            citation_index = int(numbered.group(1))
            if citation_map is None or citation_index not in citation_map:
                return []
            pairs.append(citation_map[citation_index])
            continue
        if "|" in raw_reference:
            if raw_reference.count("|") != 1:
                return []
            source_file, chunk_id = (part.strip() for part in raw_reference.split("|", 1))
        elif "source_file=" in raw_reference and "chunk_id=" in raw_reference:
            fields = {}
            for raw_field in raw_reference.split(";"):
                if "=" not in raw_field:
                    return []
                key, value = (part.strip() for part in raw_field.split("=", 1))
                fields[key] = value
            source_file = fields.get("source_file", "")
            chunk_id = fields.get("chunk_id", "")
        elif "#" not in raw_reference:
            continue
        else:
            return []
        if not source_file or not chunk_id:
            return []
        pairs.append((source_file, chunk_id))
    return pairs


def is_explicit_knowledge_refusal(answer: str) -> bool:
    """Recognize a grounded model refusal without estimating from text length."""
    normalized = "".join(str(answer or "").lower().split())
    markers = (
        "当前知识库无法回答",
        "无法基于当前的知识库回答",
        "无法从当前知识库确认",
        "知识库中没有",
        "没有关于",
        "未涉及",
        "无相关信息",
    )
    return any(marker in normalized for marker in markers)


def message_content_to_text(content: Any) -> str:
    """Render LangChain message content into a string response."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def copy_message_with_content(message: BaseMessage, content: str) -> BaseMessage:
    """Return a copy of a LangChain message with replaced content."""
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    if hasattr(message, "copy"):
        return message.copy(update={"content": content})
    if isinstance(message, HumanMessage):
        return HumanMessage(content=content)
    return message


def build_no_answer_message(retrieval_payload: dict[str, Any]) -> str:
    """Return a stable refusal message when no trusted RAG source is available."""
    summary = str(retrieval_payload.get("summary") or NO_TRUSTED_KNOWLEDGE)
    if retrieval_payload.get("status") == "failed":
        return f"{summary}。知识库检索暂不可用，请检查向量库或检索配置后重试。"

    rejected = retrieval_payload.get("rejected_results") or []
    suffix = ""
    if rejected:
        suffix = "\n\n已检索到候选片段，但距离分数超过可信阈值，已拒绝强答。"
    return f"{summary}请补充相关知识库文档后再提问。{suffix}"


def build_missing_citation_message() -> str:
    """Return a stable refusal when trusted chunks cannot be cited."""
    return (
        "检索到候选知识，但缺少可审计引用信息，已拒绝生成回答。"
        "请重新索引文档或补齐 source_file 与 chunk_id 后再提问。"
    )


def build_citation_guard_payload(retrieval_payload: dict[str, Any]) -> dict[str, Any]:
    """Mark a successful retrieval as unusable when citations are incomplete."""
    guarded = dict(retrieval_payload)
    guarded["status"] = "no_answer"
    guarded["summary"] = "检索到候选知识，但缺少可审计引用信息。"
    guarded["answer_policy"] = "refuse_without_citation"
    guarded["no_answer_rejected"] = True
    guarded["rejected_results"] = list(guarded.get("retrieval_results") or [])
    guarded["retrieval_results"] = []
    return guarded


def has_valid_citations(citations: list[dict[str, Any]]) -> bool:
    """Require every supplied citation to include a stable source and chunk identity."""
    if not citations:
        return False
    seen_indices: set[int] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for item in citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if not source_file or source_file == "未知来源" or not chunk_id:
            return False
        pair = (source_file, chunk_id)
        if pair in seen_pairs:
            return False
        seen_pairs.add(pair)
        raw_index = item.get("citation_index")
        if raw_index is not None:
            if isinstance(raw_index, bool):
                return False
            try:
                citation_index = int(raw_index)
            except (TypeError, ValueError):
                return False
            if citation_index <= 0 or citation_index in seen_indices:
                return False
            seen_indices.add(citation_index)
    return True


def ensure_citation_block(answer: str, citations: list[dict[str, Any]]) -> str:
    """Append missing source_file/chunk_id references so grounded answers stay auditable."""
    clean_answer = str(answer or "").strip()
    if not citations:
        return clean_answer

    cited_pairs = set(
        extract_citation_pairs(
            clean_answer,
            citation_map=citation_pair_map(citations),
        )
    )
    missing = []
    for item in citations:
        source_file = str(item.get("source_file") or "未知来源")
        chunk_id = str(item.get("chunk_id") or "unknown")
        if (source_file, chunk_id) not in cited_pairs:
            missing.append((source_file, chunk_id, item.get("score"), item))

    if not missing:
        return clean_answer

    lines = ["", "", "引用来源："]
    for source_file, chunk_id, score, item in missing:
        score_text = format_score(score)
        locator_parts = [f"source_file: {source_file}", f"chunk_id: {chunk_id}"]
        if item.get("page_number") is not None:
            locator_parts.append(f"page_number: {item.get('page_number')}")
        if item.get("sheet_name"):
            locator_parts.append(f"sheet_name: {item.get('sheet_name')}")
        if item.get("row_number") is not None:
            locator_parts.append(f"row_number: {item.get('row_number')}")
        if item.get("primary_key"):
            locator_parts.append(f"primary_key: {item.get('primary_key')}")
        locator_parts.append(f"score: {score_text}")
        lines.append("- " + "; ".join(locator_parts))
    return clean_answer + "\n".join(lines)
