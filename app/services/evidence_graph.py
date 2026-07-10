"""Incident evidence graph read model for diagnosis reports."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.utils.structured_data import as_dict

LIVE_EVIDENCE_SOURCES = {
    "redis_info",
    "mysql",
    "prometheus",
    "loki",
    "log_gateway",
    "kubernetes",
}
LIVE_EVIDENCE_TOOLS = {
    "query_redis_status",
    "query_mysql_status",
    "query_metrics",
    "query_logs",
    "query_k8s_status",
}
LIVE_EVIDENCE_TYPES = {
    "redis",
    "mysql",
    "metric",
    "log",
    "k8s",
}
KNOWLEDGE_EVIDENCE_TOOLS = {
    "search_runbook",
    "retrieve_runbook",
    "retrieve_knowledge",
}
KNOWLEDGE_EVIDENCE_TYPES = {
    "runbook",
    "knowledge",
}
KNOWLEDGE_DOC_SUFFIXES = (
    ".md",
    ".markdown",
    ".pdf",
    ".html",
    ".htm",
)
HISTORY_EVIDENCE_SOURCES = {
    "ticket_api",
    "deploy_history",
}
HISTORY_EVIDENCE_TOOLS = {
    "search_history_ticket",
    "query_deploy_history",
}
HISTORY_EVIDENCE_TYPES = {
    "ticket",
    "deploy_history",
    "change",
}
HISTORY_DOC_SUFFIXES = (
    ".csv",
    ".xlsx",
)


def build_incident_evidence_graph(
    *,
    incident_id: str,
    trace_id: str,
    root_cause: str,
    selected_root_cause_id: str,
    hypothesis_ranking: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    conclusion_alignment: dict[str, Any],
) -> dict[str, Any]:
    """Build a portable graph that links incident, RCA, evidence, tools, and citations."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    def add_node(node: dict[str, Any]) -> None:
        node_id = str(node.get("node_id") or "").strip()
        if not node_id or node_id in node_ids:
            return
        node_ids.add(node_id)
        nodes.append(node)

    def add_edge(source: str, target: str, relation: str, **metadata: Any) -> None:
        if not source or not target:
            return
        edge = {
            "source": source,
            "target": target,
            "relation": relation,
            **{key: value for key, value in metadata.items() if value not in (None, "", [])},
        }
        edges.append(edge)

    incident_node_id = f"incident:{incident_id}"
    add_node(
        {
            "node_id": incident_node_id,
            "node_type": "incident",
            "label": incident_id,
            "incident_id": incident_id,
            "trace_id": trace_id,
        }
    )

    evidence_by_id = {
        str(item.get("evidence_id")): item for item in evidence if item.get("evidence_id")
    }
    root_supporting_ids = _root_supporting_evidence_ids(
        selected_root_cause_id=selected_root_cause_id,
        hypothesis_ranking=hypothesis_ranking,
        evidence=evidence,
        conclusion_alignment=conclusion_alignment,
    )

    for index, hypothesis in enumerate(hypothesis_ranking[:8], 1):
        hypothesis_id = str(hypothesis.get("hypothesis_id") or f"hyp-{index}")
        node_id = f"hypothesis:{hypothesis_id}"
        add_node(
            {
                "node_id": node_id,
                "node_type": "hypothesis",
                "label": hypothesis.get("title")
                or hypothesis.get("description")
                or f"hypothesis {index}",
                "hypothesis_id": hypothesis_id,
                "selected": hypothesis_id == selected_root_cause_id
                or (not selected_root_cause_id and index == 1),
                "category": hypothesis.get("category", "unknown"),
                "confidence": float(hypothesis.get("confidence") or 0.0),
                "confidence_reason": hypothesis.get("confidence_reason", ""),
            }
        )
        add_edge(incident_node_id, node_id, "has_hypothesis", rank=index)
        for evidence_id in _string_list(hypothesis.get("supporting_evidence_ids")):
            add_edge(node_id, f"evidence:{evidence_id}", "supported_by")
        for evidence_id in _string_list(hypothesis.get("refuting_evidence_ids")):
            add_edge(node_id, f"evidence:{evidence_id}", "refuted_by")

    for item in evidence:
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        evidence_node_id = f"evidence:{evidence_id}"
        layer = evidence_layer(item)
        add_node(
            {
                "node_id": evidence_node_id,
                "node_type": "evidence",
                "label": _evidence_label(item),
                "evidence_id": evidence_id,
                "layer": layer,
                "source_tool": item.get("source_tool", "unknown"),
                "data_source": item.get("data_source", "unknown"),
                "evidence_type": item.get("evidence_type", "unknown"),
                "stance": item.get("stance", "neutral"),
                "confidence": float(item.get("confidence") or 0.0),
                "status": as_dict(item.get("raw_data")).get("status", "unknown"),
                "is_root_cause_support": evidence_id in root_supporting_ids,
            }
        )
        add_edge(evidence_node_id, incident_node_id, "observed_for")
        tool_node_id = _tool_node_id_for_evidence(item, tool_calls)
        if tool_node_id:
            add_edge(evidence_node_id, tool_node_id, "produced_by")
        for citation in _evidence_citations(item):
            citation_node_id = f"citation:{citation['source_file']}#{citation['chunk_id']}"
            add_node(
                {
                    "node_id": citation_node_id,
                    "node_type": "citation",
                    "label": f"{citation['source_file']}#{citation['chunk_id']}",
                    **citation,
                }
            )
            add_edge(evidence_node_id, citation_node_id, "grounded_in")

    for call in tool_calls:
        tool_name = str(call.get("tool_name") or "unknown")
        step_id = str(call.get("step_id") or "")
        call_id = str(call.get("call_id") or step_id or tool_name)
        node_id = f"tool:{call_id}"
        add_node(
            {
                "node_id": node_id,
                "node_type": "tool_call",
                "label": tool_name,
                "tool_name": tool_name,
                "step_id": step_id,
                "status": call.get("status", "unknown"),
                "data_source": call.get("data_source", "unknown"),
                "latency_ms": call.get("latency_ms", 0.0),
                "read_only": call.get("read_only", True),
            }
        )
        add_edge(incident_node_id, node_id, "investigated_by")

    closure = _root_cause_closure(
        root_cause=root_cause,
        selected_root_cause_id=selected_root_cause_id,
        root_supporting_ids=root_supporting_ids,
        evidence_by_id=evidence_by_id,
    )
    return {
        "graph_id": f"eg:{incident_id}:{trace_id or 'no-trace'}",
        "incident_id": incident_id,
        "trace_id": trace_id,
        "root_cause": root_cause,
        "selected_root_cause_id": selected_root_cause_id,
        "nodes": nodes,
        "edges": edges,
        "root_cause_closure": closure,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "evidence_count": len(evidence_by_id),
            "tool_call_count": len(tool_calls),
            "evidence_by_layer": dict(Counter(evidence_layer(item) for item in evidence)),
            "evidence_by_stance": dict(
                Counter(str(item.get("stance") or "neutral") for item in evidence)
            ),
        },
    }


def evidence_layer(item: dict[str, Any]) -> str:
    """Classify evidence for interview-friendly graph reading."""
    source = str(item.get("data_source") or "").lower()
    tool = str(item.get("source_tool") or "").lower()
    evidence_type = str(item.get("evidence_type") or "").lower()
    source_files = _evidence_source_files(item)

    if (
        source in LIVE_EVIDENCE_SOURCES
        or tool in LIVE_EVIDENCE_TOOLS
        or evidence_type in LIVE_EVIDENCE_TYPES
    ):
        return "live"
    if (
        source in HISTORY_EVIDENCE_SOURCES
        or tool in HISTORY_EVIDENCE_TOOLS
        or evidence_type in HISTORY_EVIDENCE_TYPES
        or any(_has_suffix(path, HISTORY_DOC_SUFFIXES) for path in source_files)
    ):
        return "history"
    if (
        tool in KNOWLEDGE_EVIDENCE_TOOLS
        or evidence_type in KNOWLEDGE_EVIDENCE_TYPES
        or any(_has_suffix(path, KNOWLEDGE_DOC_SUFFIXES) for path in source_files)
    ):
        return "knowledge"
    return "other"


def _root_supporting_evidence_ids(
    *,
    selected_root_cause_id: str,
    hypothesis_ranking: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    conclusion_alignment: dict[str, Any],
) -> set[str]:
    ids: list[str] = []
    for item in hypothesis_ranking:
        if selected_root_cause_id and item.get("hypothesis_id") != selected_root_cause_id:
            continue
        ids.extend(_string_list(item.get("supporting_evidence_ids")))
        if selected_root_cause_id:
            break
    root_field = as_dict(as_dict(conclusion_alignment.get("fields")).get("root_cause"))
    ids.extend(_string_list(root_field.get("evidence_ids")))
    if not ids:
        ids.extend(
            str(item.get("evidence_id"))
            for item in evidence
            if item.get("evidence_id") and item.get("stance") == "supporting"
        )
    return {item for item in ids if item}


def _root_cause_closure(
    *,
    root_cause: str,
    selected_root_cause_id: str,
    root_supporting_ids: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    linked = [
        evidence_by_id[evidence_id]
        for evidence_id in root_supporting_ids
        if evidence_id in evidence_by_id
    ]
    live_ids = [str(item.get("evidence_id")) for item in linked if evidence_layer(item) == "live"]
    knowledge_ids = [
        str(item.get("evidence_id")) for item in linked if evidence_layer(item) == "knowledge"
    ]
    history_ids = [
        str(item.get("evidence_id")) for item in linked if evidence_layer(item) == "history"
    ]
    missing_layers: list[str] = []
    if not live_ids:
        missing_layers.append("live")
    if not knowledge_ids and not history_ids:
        missing_layers.append("knowledge_or_history")
    return {
        "status": "closed" if not missing_layers else "incomplete",
        "root_cause": root_cause,
        "selected_root_cause_id": selected_root_cause_id,
        "required_layers": ["live", "knowledge_or_history"],
        "supporting_evidence_ids": sorted(root_supporting_ids),
        "live_evidence_ids": live_ids,
        "knowledge_evidence_ids": knowledge_ids,
        "history_evidence_ids": history_ids,
        "missing_layers": missing_layers,
    }


def _tool_node_id_for_evidence(evidence: dict[str, Any], tool_calls: list[dict[str, Any]]) -> str:
    evidence_tool = str(evidence.get("source_tool") or "")
    evidence_step = str(evidence.get("step_id") or "")
    for call in tool_calls:
        step_id = str(call.get("step_id") or "")
        tool_name = str(call.get("tool_name") or "")
        if evidence_step and step_id == evidence_step:
            return f"tool:{call.get('call_id') or step_id or tool_name}"
        if evidence_tool and tool_name == evidence_tool:
            return f"tool:{call.get('call_id') or step_id or tool_name}"
    return ""


def _evidence_label(item: dict[str, Any]) -> str:
    text = (
        str(item.get("fact") or "").strip()
        or str(item.get("summary") or "").strip()
        or str(item.get("inference") or "").strip()
        or str(item.get("evidence_id") or "evidence")
    )
    return text[:160] + "..." if len(text) > 160 else text


def _evidence_citations(item: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for payload in _candidate_retrieval_payloads(item):
        for result in payload.get("retrieval_results", []) or []:
            if not isinstance(result, dict):
                continue
            source_file = str(result.get("source_file") or "").strip()
            chunk_id = str(result.get("chunk_id") or "").strip()
            if not source_file or not chunk_id:
                continue
            key = f"{source_file}#{chunk_id}"
            if key in seen:
                continue
            seen.add(key)
            citations.append({"source_file": source_file, "chunk_id": chunk_id})
    return citations[:8]


def _candidate_retrieval_payloads(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw_data = as_dict(item.get("raw_data"))
    output = as_dict(raw_data.get("output"))
    payloads = []
    if raw_data.get("retrieval_results"):
        payloads.append(raw_data)
    if output.get("retrieval_results"):
        payloads.append(output)
    return payloads


def _evidence_source_files(item: dict[str, Any]) -> list[str]:
    files: list[str] = []
    output = as_dict(as_dict(item.get("raw_data")).get("output")) or as_dict(item.get("raw_data"))
    for key in ("source_file", "source_path", "file_name", "path"):
        value = output.get(key)
        if value:
            files.append(str(value))
    for payload in _candidate_retrieval_payloads(item):
        for result in payload.get("retrieval_results", []) or []:
            if not isinstance(result, dict):
                continue
            for key in ("source_file", "source_path", "file_name", "path"):
                value = result.get(key)
                if value:
                    files.append(str(value))
            metadata = as_dict(result.get("metadata"))
            for key in ("source_file", "source_path", "_source"):
                value = metadata.get(key)
                if value:
                    files.append(str(value))
    return files


def _has_suffix(value: str, suffixes: tuple[str, ...]) -> bool:
    return value.lower().strip().endswith(suffixes)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
