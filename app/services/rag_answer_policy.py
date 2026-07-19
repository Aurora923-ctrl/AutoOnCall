"""Answer-policy helpers for grounded RAG responses."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.context_budget import DEFAULT_CONTEXT_BUDGETER, ContextBudgeter
from app.services.rag_read_models import format_score
from app.services.rag_retrieval_service import NO_TRUSTED_KNOWLEDGE


def build_grounded_question(question: str, retrieval_payload: dict[str, Any]) -> str:
    """Build the final LLM prompt from trusted retrieval context."""
    context = build_generation_context(retrieval_payload)
    return (
        "请只基于下面的知识库检索结果回答用户问题。"
        "不要使用未出现在知识库中的事实；如果知识不足或主题不匹配，请明确说明"
        "“当前知识库无法回答该问题”，不要引用无关片段。\n\n"
        f"{context}\n\n"
        f"用户问题: {question}\n\n"
        "回答要求:\n"
        "0. 在适用时按“已知上下文事实 / 当前事故仍需查询的证据 / "
        "允许的处置建议与安全边界 / 不确定项”组织答案；不要为了凑齐栏目而扩写。\n"
        "1. 按用户问题中的子问题逐项回答，最多写 4 条要点；不要遗漏明确询问的取证、"
        "判断或处置边界。\n"
        "2. 不复述整份 Runbook，不补充用户未询问的步骤、通用建议或常识。\n"
        "3. 命令、参数、数值、原因和动作必须由证据片段直接提供；允许忠实转述，"
        "但不得推导新的因果关系或操作。\n"
        "4. 静态 Runbook 只能写成“建议检查”，不得写成当前事件已经观测到的事实。\n"
        "5. 处置动作仅在片段明确支持时保留，并原样保留审批、验证或回滚边界。\n"
        "6. 每条要点末尾标注唯一直接支持它的 [source_file | chunk_id]；"
        "不要列出未被正文使用的来源。\n"
        "7. 只要至少一个片段能回答部分问题，就回答有证据的部分；不要因为缺少另一部分"
        "而整体拒答。\n"
        "8. 不得把通配符、示例值或占位符替换成用户问题中的服务名、阈值或参数。\n"
        "9. 不得从“需要检查某项证据”推导“满足该证据后即可重启、扩容、清理或回滚”；"
        "动作条件必须由片段直接给出。\n"
        "10. 告警名只能作为检查线索，不能单独推出原因、文件特征或处理方案。\n"
        "11. 当片段只能回答部分问题时，先给出有证据的要点，最后用一句“当前片段未提供"
        "其余问题的依据”说明具体缺口；只有确实存在未回答子问题时才写这句，已经完整"
        "回答时禁止追加泛化缺口；不要使用“知识库无法回答”这类整体拒答措辞。\n"
        "12. 输出前逐项检查证据覆盖：问题要求多个来源时，每个必要来源至少支持一条要点；"
        "片段已提供审批、dry-run、验证、回滚或人工接管边界时，必须在相关动作或判断中"
        "保留该边界。\n"
        "13. 命令、标签选择器、路径、IP、端口、通配符和占位符必须按片段原样引用；"
        "不得替换成新的示例值，也不要要求用户把片段中的示例改成实际值。\n"
        "14. 对调查型问题优先写成“检查项 -> 如何判断 -> 证据边界”；不要把文档中的"
        "示例成功输出写成当前环境的预期事实。"
    )


def build_grounded_system_prompt() -> str:
    """Return the strict system prompt used for tool-free grounded generation."""
    return (
        "回答在适用时固定分为四部分：已知上下文事实、当前事故仍需查询的证据、"
        "允许的处置建议与安全边界、不确定项。"
        "每个事实或操作 claim 末尾必须绑定且仅绑定一个允许列表内的 "
        "[source_file | chunk_id] 引用。"
        "Runbook 中的指标、阈值、示例值和历史观测只能表述为静态文档信息，"
        "不得写成当前事故的现场观测。"
        "上下文只能支持有限回答时，给出有限回答和明确缺失的证据，禁止依靠通用知识扩写。"
        "你是知识库问答助手。你不能调用工具，也不能补充知识库之外的事实。"
        "只能复述或紧密归纳当前检索片段已明确提供的信息。"
        "不得引入检索片段中未出现的命令、工具名、监控方法、参数或处置动作。"
        "将每个句子视为需要证据支持的独立 claim；无法指出支持片段就删除该句。"
        "答案最多 4 条要点，并覆盖用户明确询问的证据、判断和边界。"
        "只要有片段能支持部分答案，就回答该部分，不要整体拒答。"
        "保留片段中的通配符和示例值，不得用用户问题中的实体替换。"
        "不得从诊断证据推导动作资格，也不得从告警名称扩写原因或处理方案。"
        "部分证据不足时只声明具体缺口，不要使用整体知识库拒答措辞。"
        "回答前检查问题的每个子问题和每个必要来源是否都有直接证据覆盖。"
        "检索片段已经包含审批、dry-run、验证、回滚或人工接管边界时不得遗漏。"
        "命令、选择器、路径、IP、端口、通配符和占位符必须原样引用，不得改写示例。"
        "只有确实存在未回答子问题时才能声明具体缺口，完整回答不得追加泛化缺口。"
        "如果检索片段不足以回答或与问题主题不匹配，请明确说明当前知识库无法回答，"
        "并且不要引用无关片段。"
    )


def build_generation_context(retrieval_payload: dict[str, Any]) -> str:
    """Build a de-duplicated evidence block without changing retrieval results."""
    evidence = build_generation_evidence(retrieval_payload)
    if not evidence:
        return ""
    return "\n\n".join(
        _format_evidence_block(
            index,
            source_file=str(item.get("source_file") or "未知来源").strip(),
            chunk_id=str(item.get("chunk_id") or "unknown").strip(),
            content=str(item.get("content") or item.get("content_preview") or "").strip(),
        )
        for index, item in enumerate(evidence, 1)
    )


def build_generation_evidence(
    retrieval_payload: dict[str, Any],
    *,
    budgeter: ContextBudgeter | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Freeze the de-duplicated, budgeted evidence set used for generation."""
    results = retrieval_payload.get("retrieval_results") or []
    if not isinstance(results, list) or not results:
        return []

    allowlist = {
        (
            str(item.get("source_file") or "").strip(),
            str(item.get("chunk_id") or "").strip(),
        )
        for item in retrieval_payload.get("generation_allowlist", []) or []
        if isinstance(item, dict)
        and str(item.get("source_file") or "").strip()
        and str(item.get("chunk_id") or "").strip()
    }
    evidence: list[dict[str, Any]] = []
    seen_content: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        identity = (
            str(item.get("source_file") or "").strip(),
            str(item.get("chunk_id") or "").strip(),
        )
        if allowlist and identity not in allowlist:
            continue
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        normalized = normalize_evidence_text(content)
        if not content:
            continue
        redundant_index = next(
            (
                index
                for index, previous in enumerate(seen_content)
                if evidence_texts_are_redundant(normalized, previous)
            ),
            None,
        )
        if redundant_index is not None:
            if len(normalized) <= len(seen_content[redundant_index]):
                continue
            seen_content[redundant_index] = normalized
            evidence[redundant_index] = dict(item)
            continue
        seen_content.append(normalized)
        evidence.append(dict(item))

    active_budgeter = budgeter or DEFAULT_CONTEXT_BUDGETER
    max_chars = active_budgeter.limit(limit)
    required_sources = {
        str(source).strip().lower()
        for source in retrieval_payload.get("required_sources", []) or []
        if str(source).strip()
    }
    ordered_evidence = list(evidence)
    if required_sources:
        reserved: list[dict[str, Any]] = []
        reserved_ids: set[tuple[str, str]] = set()
        for source in sorted(required_sources):
            candidate = next(
                (
                    item
                    for item in ordered_evidence
                    if citation_source_basename(item.get("source_file")) == source
                ),
                None,
            )
            if candidate is None:
                return []
            identity = (
                str(candidate.get("source_file") or "").strip(),
                str(candidate.get("chunk_id") or "").strip(),
            )
            if identity not in reserved_ids:
                reserved.append(candidate)
                reserved_ids.add(identity)
        ordered_evidence = reserved + [
            item
            for item in ordered_evidence
            if (
                str(item.get("source_file") or "").strip(),
                str(item.get("chunk_id") or "").strip(),
            )
            not in reserved_ids
        ]

    selected: list[dict[str, Any]] = []
    used_chars = 0
    selected_sources: set[str] = set()
    for item in ordered_evidence:
        source_file = str(item.get("source_file") or "未知来源").strip()
        chunk_id = str(item.get("chunk_id") or "unknown").strip()
        content = str(item.get("content") or item.get("content_preview") or "").strip()
        separator_chars = 2 if selected else 0
        header = _format_evidence_block(
            len(selected) + 1,
            source_file=source_file,
            chunk_id=chunk_id,
            content="",
        )
        remaining = max_chars - used_chars - separator_chars
        if remaining <= len(header):
            break
        if len(header) + len(content) <= remaining:
            selected.append(dict(item))
            selected_sources.add(citation_source_basename(item.get("source_file")))
            used_chars += separator_chars + len(header) + len(content)
            continue
        if (
            required_sources
            and citation_source_basename(item.get("source_file")) in required_sources
        ):
            return []
        if selected:
            break
        marker = active_budgeter.budget.truncation_marker
        available_content = remaining - len(header)
        if available_content <= len(marker):
            break
        truncated = active_budgeter.text(
            content,
            limit=available_content,
            preserve_tail=True,
        )
        trimmed_item = dict(item)
        if item.get("content"):
            trimmed_item["content"] = truncated
        else:
            trimmed_item["content_preview"] = truncated
        selected.append(trimmed_item)
        selected_sources.add(citation_source_basename(trimmed_item.get("source_file")))
        break
    if required_sources and not required_sources.issubset(selected_sources):
        return []
    return normalize_generation_citation_labels(selected)


def citation_source_basename(value: Any) -> str:
    """Normalize a source label for required-source and generation checks."""
    return str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()


def normalize_generation_citation_labels(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use basenames when unique, preserving relative paths for real collisions."""
    citation_counts: dict[tuple[str, str], int] = {}
    for item in evidence:
        source_file = str(item.get("source_file") or "").strip().replace("\\", "/")
        citation = (source_file.rsplit("/", 1)[-1], str(item.get("chunk_id") or "").strip())
        citation_counts[citation] = citation_counts.get(citation, 0) + 1

    normalized = []
    for item in evidence:
        updated = dict(item)
        source_file = str(updated.get("source_file") or "").strip().replace("\\", "/")
        basename = source_file.rsplit("/", 1)[-1]
        citation = (basename, str(updated.get("chunk_id") or "").strip())
        if citation_counts.get(citation) == 1:
            updated["source_file"] = basename
        normalized.append(updated)
    return normalized


def normalize_evidence_text(content: str) -> str:
    """Normalize formatting differences before comparing retrieved chunks."""
    return re.sub(r"\s+", " ", str(content or "")).strip().lower()


def evidence_texts_are_redundant(left: str, right: str) -> bool:
    """Treat exact and near-duplicate copies as one generation-time evidence block."""
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < 50:
        return False
    return shorter in longer and len(shorter) / len(longer) >= 0.65


def _format_evidence_block(
    index: int,
    *,
    source_file: str,
    chunk_id: str,
    content: str,
) -> str:
    return f"[证据 {index}: source_file={source_file}; chunk_id={chunk_id}]\n{content}"


def select_supporting_citations(
    answer: str,
    citations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep exact allowlisted source/chunk pairs and reject any fabricated pair."""
    allowed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if source_file and chunk_id:
            allowed.setdefault((source_file, chunk_id), item)

    cited_pairs = extract_citation_pairs(answer)
    if not cited_pairs or any(pair not in allowed for pair in cited_pairs):
        return []
    if not answer_claims_are_cited(answer, allowed_pairs=set(allowed)):
        return []

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pair in cited_pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(allowed[pair])
    return unique


def remove_generic_uncertainty_boilerplate(answer: str) -> str:
    """Drop the unsupported catch-all gap sentence before citation validation."""
    kept_lines = []
    for line in str(answer or "").splitlines():
        normalized = re.sub(r"^\s*(?:[-*]|\d+[.)])?\s*", "", line).strip()
        normalized = re.sub(r"^不确定项\s*[:：]\s*", "", normalized).strip()
        if normalized == "当前片段未提供其余问题的依据。":
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip()


def answer_claims_are_cited(
    answer: str,
    *,
    allowed_pairs: set[tuple[str, str]],
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
        pairs = extract_citation_pairs(line)
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
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines or not allowed:
        return False
    for line in lines:
        pairs = extract_citation_pairs(line)
        if pairs and len(pairs) == 1 and pairs[0] in allowed:
            continue
        if line.endswith(("。", ".", "!", "！", "?", "？", ":", "：")):
            return False
    return True


def extract_citation_pairs(answer: str) -> list[tuple[str, str]]:
    """Parse allowlist-bound citation references from a grounded answer."""
    pairs: list[tuple[str, str]] = []
    for raw_reference in re.findall(r"\[([^\[\]\r\n]+)\]", str(answer or "")):
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
    for item in citations:
        source_file = str(item.get("source_file") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if not source_file or source_file == "未知来源" or not chunk_id:
            return False
    return True


def ensure_citation_block(answer: str, citations: list[dict[str, Any]]) -> str:
    """Append missing source_file/chunk_id references so grounded answers stay auditable."""
    clean_answer = str(answer or "").strip()
    if not citations:
        return clean_answer

    cited_pairs = set(extract_citation_pairs(clean_answer))
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
