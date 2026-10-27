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
    """按预设脚本模拟模型回答，工具由循环执行。

    输入：与真实模型相同的请求接口；输出：工具请求或包含实际结果的文字。
    流程：先请求 project_name，收到工具结果后回答；不访问网络、不自行选择或执行工具。
    """

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
        # s01 修改：直接展示预设的模型回答，去掉不必要的工具名与参数配置。
        return AssistantMessage(
            (ToolCall(f"demo-{len(messages)}", "project_name", {}),), "tool_use"
        )


# s01 新增：无参数工具只返回调用方已经选择的工作目录名称。
def project_name_tool(workspace: Path) -> Tool:
    """绑定工作目录，创建本章实际使用的无参数异步工具。"""

    async def execute(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(workspace.name)

    return Tool("project_name", "查看当前项目名称", execute)
