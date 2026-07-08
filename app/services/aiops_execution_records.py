"""Build persistent AIOps execution records from tool results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.config import config
from app.models.evidence import (
    Evidence,
    build_confidence_reason,
    infer_evidence_stance,
    infer_evidence_type,
    normalize_data_source,
)
from app.models.plan import PlanStep
from app.models.trace import ToolCallRecord
from app.services.aiops_state_utils import extract_incident_id
from app.tools.base import ToolExecutionResult
from app.utils.redaction import redact_sensitive_data


def result_for_persistence(result: ToolExecutionResult) -> ToolExecutionResult:
    """Return a copy with bulky external raw payloads removed for Trace/Report storage."""
    redacted_args = redact_sensitive_data(result.input_args)
    redacted_output = redact_sensitive_data(result.output)
    updates: dict[str, Any] = {}
    if redacted_args != result.input_args:
        updates["input_args"] = redacted_args
    if redacted_output != result.output:
        updates["output"] = redacted_output
    persisted_result = result.model_copy(update=updates) if updates else result
    if not config.aiops_store_raw_external_payload and isinstance(persisted_result.output, dict):
        compact_output = redact_sensitive_data(_compact_external_payload(persisted_result.output))
        if compact_output is not persisted_result.output:
            persisted_result = persisted_result.model_copy(update={"output": compact_output})
    return _materialize_large_output_artifact(persisted_result)


def tool_result_to_evidence(result: ToolExecutionResult, step: PlanStep) -> Evidence:
    """Convert a tool result into audit-ready diagnostic evidence."""
    raw_data = result.model_dump(mode="json")
    summary = summarize_tool_result(result)
    stance = infer_evidence_stance(
        source_tool=result.tool_name,
        raw_data=raw_data,
        summary=summary,
    )
    data_source = normalize_data_source(result.tool_name, raw_data)
    confidence = _evidence_confidence(result, data_source)
    execution_path = result.metadata.get("execution_path")
    if execution_path == "manual_analysis":
        confidence = min(confidence, 0.35)
    elif execution_path == "llm_toolnode_fallback":
        confidence = 0.1 if result.status == "failed" else min(confidence, 0.35)

    return Evidence(
        source_tool=result.tool_name,
        step_id=step.step_id,
        summary=summary,
        evidence_type=infer_evidence_type(result.tool_name),
        data_source=data_source,
        stance=stance,
        confidence_reason=build_confidence_reason(
            source_tool=result.tool_name,
            raw_data=raw_data,
            stance=stance,
        ),
        fact=_build_evidence_fact(result, data_source),
        inference=_build_evidence_inference(result, stance),
        uncertainty=_build_evidence_uncertainty(result, data_source),
        next_step=_build_evidence_next_step(result, data_source, step),
        raw_data=raw_data,
        artifact_refs=_artifact_refs_from_result(result),
        confidence=confidence,
        related_hypothesis=step.expected_evidence,
    )


def tool_result_to_call_record(
    result: ToolExecutionResult,
    step: PlanStep,
    state: Mapping[str, Any],
) -> ToolCallRecord:
    """Convert a tool result into a replayable tool call audit record."""
    return ToolCallRecord(
        trace_id=state.get("trace_id") or "trace-unknown",
        incident_id=extract_incident_id(state),
        step_id=step.step_id,
        tool_name=result.tool_name,
        input_args=result.input_args,
        input_summary=summarize_input_args(result.input_args),
        output=result.output,
        output_artifact=_artifact_ref_from_result(result),
        output_summary=summarize_tool_result(result),
        data_source=normalize_data_source(result.tool_name, result.model_dump(mode="json")),
        latency_ms=result.latency_ms,
        status=result.status,
        risk_level=result.risk_level,
        read_only=result.read_only,
        error_message=result.error_message,
    )


def summarize_tool_result(result: ToolExecutionResult) -> str:
    """Create a compact human-readable evidence summary."""
    if result.status == "failed":
        return f"工具 {result.tool_name} 调用失败: {result.error_message or '未知错误'}"

    output = result.output
    if isinstance(output, dict):
        summary = output.get("summary")
        if summary:
            return str(summary)
        return f"工具 {result.tool_name} 返回 {len(output)} 个结构化字段"
    if isinstance(output, list):
        return f"工具 {result.tool_name} 返回 {len(output)} 条记录"
    if output is None:
        return f"工具 {result.tool_name} 调用成功，但未返回输出"

    text = str(output).strip()
    return text[:300] if len(text) > 300 else text


def summarize_input_args(input_args: dict[str, Any]) -> str:
    """Create a compact, non-secret input summary for tool audit displays."""
    if not input_args:
        return "无输入参数"
    safe_args = redact_sensitive_data(input_args)
    text = json.dumps(safe_args, ensure_ascii=False, default=str)
    return text[:220] + "..." if len(text) > 220 else text


def format_tool_error(tool_call_record: dict[str, Any]) -> str:
    """Render failed tool call as a state error string."""
    return (
        f"工具 {tool_call_record.get('tool_name')} "
        f"步骤 {tool_call_record.get('step_id')} 调用失败: "
        f"{tool_call_record.get('error_message') or '未知错误'}"
    )


def _compact_external_payload(output: dict[str, Any]) -> dict[str, Any]:
    source = str(output.get("source") or "")
    if source not in {
        "prometheus",
        "loki",
        "log_gateway",
        "cmdb",
        "deploy_history",
        "redis_info",
        "kubernetes",
        "mysql",
        "ticket_api",
    }:
        return output
    compact = dict(output)
    raw = compact.get("raw")
    if isinstance(raw, dict):
        compact["raw"] = _compact_raw_payload(raw)
        compact["raw_truncated"] = True
    return compact


def _materialize_large_output_artifact(result: ToolExecutionResult) -> ToolExecutionResult:
    """Store oversized tool output as a redacted artifact and keep only a compact pointer."""
    output = result.output
    if output is None:
        return result

    inline_limit = max(int(config.aiops_tool_output_inline_bytes or 0), 0)
    output_bytes = _json_bytes(output)
    if len(output_bytes) <= inline_limit:
        return result

    artifact = _write_tool_output_artifact(result, output, output_bytes)
    compact_output = _artifact_inline_output(result.tool_name, output, artifact)
    metadata = dict(result.metadata or {})
    metadata["output_artifact"] = artifact
    return result.model_copy(update={"output": compact_output, "metadata": metadata})


def _write_tool_output_artifact(
    result: ToolExecutionResult,
    output: Any,
    output_bytes: bytes,
) -> dict[str, Any]:
    output_sha = hashlib.sha256(output_bytes).hexdigest()
    artifact_id = f"toolout-{_safe_artifact_name(result.tool_name)}-{output_sha[:16]}"
    artifact_dir = Path(config.aiops_tool_output_artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_id}.json"
    artifact_payload = {
        "kind": "tool_output",
        "artifact_id": artifact_id,
        "tool_name": result.tool_name,
        "status": result.status,
        "input_args": result.input_args,
        "output": output,
        "output_sha256": output_sha,
        "output_size_bytes": len(output_bytes),
    }
    if not artifact_path.exists():
        artifact_path.write_text(
            json.dumps(artifact_payload, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
    return {
        "artifact_id": artifact_id,
        "kind": "tool_output",
        "artifact_ref": artifact_path.as_posix(),
        "sha256": output_sha,
        "size_bytes": len(output_bytes),
        "truncated": True,
    }


def _artifact_inline_output(
    tool_name: str,
    output: Any,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    compact = {
        "summary": _summarize_raw_output(tool_name, output),
        "artifact_ref": artifact["artifact_ref"],
        "artifact_id": artifact["artifact_id"],
        "sha256": artifact["sha256"],
        "size_bytes": artifact["size_bytes"],
        "truncated": True,
    }
    if isinstance(output, dict):
        for key in ("source", "status", "error_type"):
            if output.get(key) is not None:
                compact[key] = output.get(key)
    return compact


def _summarize_raw_output(tool_name: str, output: Any) -> str:
    if isinstance(output, dict):
        summary = output.get("summary")
        if summary:
            return str(summary)[:500]
        keys = ", ".join(str(key) for key in list(output.keys())[:12])
        return f"工具 {tool_name} 返回 {len(output)} 个结构化字段，完整输出已落盘；keys={keys}"
    if isinstance(output, list):
        return f"工具 {tool_name} 返回 {len(output)} 条记录，完整输出已落盘。"
    text = str(output).strip()
    if len(text) > 500:
        text = f"{text[:500]}..."
    return f"工具 {tool_name} 返回大文本输出，完整输出已落盘：{text}"


def _artifact_ref_from_result(result: ToolExecutionResult) -> dict[str, Any] | None:
    artifact = result.metadata.get("output_artifact") if isinstance(result.metadata, dict) else None
    return dict(artifact) if isinstance(artifact, dict) else None


def _artifact_refs_from_result(result: ToolExecutionResult) -> list[dict[str, Any]]:
    artifact = _artifact_ref_from_result(result)
    return [artifact] if artifact else []


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")


def _safe_artifact_name(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return normalized.strip("-_")[:80] or "tool"


def _compact_raw_payload(raw: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            compact[key] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_key
                in {
                    "connected_clients",
                    "blocked_clients",
                    "maxclients",
                    "used_memory",
                    "maxmemory",
                    "Threads_connected",
                    "Max_used_connections",
                    "Slow_queries",
                    "Innodb_row_lock_waits",
                }
                or str(nested_key).startswith("db")
            }
            if len(compact[key]) != len(value):
                compact[key]["_raw_truncated"] = True
        elif isinstance(value, list):
            compact[key] = value[:5]
        else:
            compact[key] = value
    return compact


def _evidence_confidence(result: ToolExecutionResult, data_source: str) -> float:
    """Score confidence from status and provenance, not just successful execution."""
    if result.status == "failed":
        return 0.05 if data_source == "not_configured" else 0.1
    if data_source in {
        "prometheus",
        "loki",
        "log_gateway",
        "cmdb",
        "deploy_history",
        "redis_info",
        "kubernetes",
        "mysql",
        "ticket_api",
    }:
        return 0.82
    if data_source in {"mcp_monitor", "mcp_cls", "rag"}:
        return 0.72
    if data_source == "mcp_monitor_mixed":
        return 0.6
    if data_source == "mock":
        return 0.5
    if data_source == "rule_based":
        return 0.65
    if data_source == "failed":
        return 0.1
    return 0.65


def _build_evidence_fact(result: ToolExecutionResult, data_source: str) -> str:
    """Separate directly observed data from later diagnostic inference."""
    if result.status == "failed":
        return f"{result.tool_name} 未返回可用数据，来源={data_source}"
    if isinstance(result.output, dict) and result.output.get("fact"):
        return str(result.output["fact"])
    summary = summarize_tool_result(result)
    return f"{summary}；来源={data_source}"


def _build_evidence_inference(result: ToolExecutionResult, stance: str) -> str:
    """Summarize what this evidence does to the active hypothesis."""
    if result.status == "failed":
        return "该步骤不能支持根因判断，只能作为证据缺口记录。"
    if isinstance(result.output, dict) and result.output.get("inference"):
        return str(result.output["inference"])
    if stance == "supporting":
        return "该证据支持当前根因假设。"
    if stance == "refuting":
        return "该证据与当前根因假设不一致，需要补充其他证据。"
    if stance == "unknown":
        return "该证据当前无法判断立场，只能作为证据缺口或待复核记录。"
    return "该证据目前只提供背景信息，尚不足以单独确认根因。"


def _build_evidence_uncertainty(result: ToolExecutionResult, data_source: str) -> str:
    """Make mock, fallback, and failure boundaries explicit."""
    if (
        result.status != "failed"
        and isinstance(result.output, dict)
        and result.output.get("uncertainty")
    ):
        return str(result.output["uncertainty"])
    if data_source == "not_configured":
        return "真实适配器未配置且 Mock 回退关闭，不能生成真实系统证据。"
    if data_source == "failed":
        return result.error_message or "真实适配器调用失败，证据不完整。"
    if data_source == "mock":
        return "该证据来自 Mock 回退，只适合本地演示，不代表真实生产状态。"
    if data_source in {"rule_based", "manual_analysis", "llm_toolnode_fallback"}:
        return "该结果来自规则或人工/LLM 兜底路径，需要结合真实工具证据复核。"
    if result.status == "failed":
        return result.error_message or "工具调用失败，证据不完整。"
    return "该证据来自当前配置的数据源，仍需结合时间窗口、采样完整性和其他工具交叉验证。"


def _build_evidence_next_step(
    result: ToolExecutionResult,
    data_source: str,
    step: PlanStep,
) -> str:
    """Recommend the next verification action based on provenance and status."""
    if data_source == "not_configured":
        return f"配置 {result.tool_name} 对应真实适配器，或开启 Mock 模式后仅作演示。"
    if result.status == "failed":
        return "检查工具配置、网络、权限或超时设置后重试。"
    if data_source == "mock":
        return "接入真实适配器后重复该步骤，确认 Mock 结论是否成立。"
    if step.expected_evidence:
        return f"用后续步骤交叉验证：{step.expected_evidence}"
    return "继续执行计划中的后续证据采集步骤。"
