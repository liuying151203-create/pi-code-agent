# ===== 来自 s02：保持 =====
# 来自 s02：保持；保存本章阶段的组件实现；相对导入只引用本章，不读取正式包或其他章节。
"""s03：以预设读取请求提供离线入口，模型协议与真实 Provider 一致。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from .messages import AssistantMessage, Message, Text, ToolCall, ToolResultMessage
from .model import ToolDefinition
from .tools import Tool, ToolResult


class DemoModel:
    """模拟读取 README 的模型回答，实际读取由循环分发给工具。

    输入：统一模型请求；输出：预设 read 请求或包含实际文件内容的回答。
    流程：请求读取 README.md → 接收工具结果 → 回答；不访问模型服务。
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
        # s03 修改：离线入口直接展示读取请求，初始化不配置工具名或工具参数。
        return AssistantMessage(
            (ToolCall(f"demo-{len(messages)}", "read", {"path": "README.md"}),), "tool_use"
        )


# 来自 s02：保持；用真正参与运算的参数展示 schema 约束和数字字符串转换。
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
