# ===== s02 新增 =====
# s02 新增；保存本章阶段的组件实现；相对导入只引用本章，不读取正式包或其他章节。
"""s02：通过预设加法请求演示参数校验、转换与结果回传。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from .messages import AssistantMessage, Message, Text, ToolCall, ToolResultMessage
from .model import ToolDefinition
from .tools import Tool, ToolResult


class DemoModel:
    """模拟带字符串参数的加法请求，实际校验和计算交给工具执行层。

    输入：统一模型请求；输出：预设 add 请求或包含实际结果的文字。
    流程：请求 add("2", "3") → 接收工具结果 → 回答；不访问网络或自行计算。
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
        # s02 修改：请求明确写在脚本回答中，字符串交给参数模型转换，不在初始化时指定。
        return AssistantMessage(
            (ToolCall(f"demo-{len(messages)}", "add", {"a": "2", "b": "3"}),), "tool_use"
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
