"""
AIOps Agent 通用工具函数
"""

from typing import Any


def format_tools_description(tools: list[Any]) -> str:
    """格式化工具列表为描述文本"""
    tool_descriptions = []

    for tool in tools:
        if hasattr(tool, "name") and hasattr(tool, "description"):
            detail_parts = []
            input_schema = getattr(tool, "input_schema", None)
            if input_schema:
                detail_parts.append(f"input_schema={input_schema}")
            risk_level = getattr(tool, "risk_level", None)
            if risk_level:
                detail_parts.append(f"risk_level={risk_level}")
            read_only = getattr(tool, "read_only", None)
            if read_only is not None:
                detail_parts.append(f"read_only={read_only}")
            data_sources = getattr(tool, "data_sources", None)
            if data_sources:
                detail_parts.append(f"data_sources={data_sources}")

            detail_text = f" ({'; '.join(detail_parts)})" if detail_parts else ""
            tool_descriptions.append(f"- {tool.name}: {tool.description}{detail_text}")

    return "\n".join(tool_descriptions)
