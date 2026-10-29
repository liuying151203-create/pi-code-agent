# ===== s08 快照 =====
# 来自 s06：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s01：无需密钥的脚本模型；s02 用它验证参数转换与工具结果回传。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .messages import AssistantMessage, Message, Text, ToolCall, ToolResultMessage
from .model import AssistantStreamEvent, ToolDefinition
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

    # 来自 s05：保持；离线脚本按固定小段模拟流；真实 Provider 直接消费供应商流，不拆完整请求冒充流式。
    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AsyncGenerator[AssistantStreamEvent, None]:
        """模拟文本事件及最终工具调用，供无密钥教学使用，不代表网络延迟。"""
        response = await self.complete(
            system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        partial = AssistantMessage((), "pending")
        yield {"type": "start", "partial": partial}
        for index, block in enumerate(response.content):
            if not isinstance(block, Text):
                continue
            text = ""
            yield {"type": "text_start", "content_index": index, "partial": partial}
            for offset in range(0, len(block.text), 5):
                delta = block.text[offset : offset + 5]
                text += delta
                partial = AssistantMessage((Text(text),), "pending")
                yield {
                    "type": "text_delta",
                    "content_index": index,
                    "delta": delta,
                    "partial": partial,
                }
            yield {
                "type": "text_end",
                "content_index": index,
                "content": block.text,
                "partial": partial,
            }
        yield {"type": "done", "message": response}


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
