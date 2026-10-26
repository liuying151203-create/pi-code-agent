# ===== s01 新增 =====
# s01 新增；只保留本章实际使用的脚本模型和无参数项目名称工具。
"""s01：无需密钥的脚本模型；只使用本章无参数工具。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .messages import AssistantMessage, Message, Text, ToolCall, ToolResultMessage
from .model import ToolDefinition
from .tools import Tool, ToolResult


class DemoModel:
    """先请求指定工具，再根据实际工具结果回答；用于离线教学和冒烟验证。"""

    def __init__(self, tool_name: str = "project_name", arguments: dict[str, Any] | None = None):
        self.tool_name = tool_name
        self.arguments = arguments if arguments is not None else {}

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AssistantMessage:
        """根据末条消息生成工具调用或包含实际结果的回答，不访问网络。"""
        if messages and isinstance(messages[-1], ToolResultMessage):
            result = messages[-1]
            label = "工具失败" if result.is_error else "工具返回"
            return AssistantMessage((Text(f"{label}：{result.content}"),))
        return AssistantMessage(
            (ToolCall(f"demo-{len(messages)}", self.tool_name, self.arguments),), "tool_use"
        )


# s01 新增：无参数工具只返回调用方已经选择的工作目录名称。
def project_name_tool(workspace: Path) -> Tool:
    """绑定工作目录，创建本章实际使用的无参数异步工具。"""

    async def execute(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(workspace.name)

    return Tool("project_name", "查看当前项目名称", execute)
