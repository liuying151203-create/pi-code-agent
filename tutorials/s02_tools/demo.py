# ===== s02 新增 =====
# s02 新增；保存本章阶段的组件实现；相对导入只引用本章，不读取正式包或其他章节。
"""s01：无需密钥的脚本模型；s02 用它验证参数转换与工具结果回传。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

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


# s02 新增：用真正参与运算的参数展示 schema 约束和数字字符串转换。
class AddArguments(BaseModel):
    """加法工具的两个整数参数，范围约束会同时进入定义和运行校验。"""

    a: int = Field(ge=-1000000, le=1000000)
    b: int = Field(ge=-1000000, le=1000000)


async def add(arguments: dict[str, Any]) -> ToolResult:
    """读取已校验的整数并返回实际求和结果。"""
    return ToolResult(str(arguments["a"] + arguments["b"]))


def add_tool() -> Tool:
    """把加法定义、参数模型和处理函数绑定到同一个可调用工具。"""
    return Tool("add", "将两个整数相加", add, AddArguments)
