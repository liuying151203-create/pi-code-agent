"""s01：仅支持当前闭环用到的无参数工具；通用参数模型在 s02 加入。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .messages import ToolCall, ToolResultMessage
from .model import ToolDefinition


# ===== s01 新增 =====
# s01 新增：成功正文和失败状态分开，不从 Error 前缀猜测结果。
@dataclass(frozen=True)
class ToolResult:
    """保存工具正文与错误标记，供循环生成独立工具结果消息。"""

    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        """拒绝无法可靠交回模型的结果。"""
        if not isinstance(self.content, str) or type(self.is_error) is not bool:
            raise TypeError("工具结果必须包含文本 content 和布尔 is_error")


# s01 新增：保持 Pi 的声明与处理函数绑定，本章只开放空参数对象；s02 再接完整校验器。
@dataclass(frozen=True)
class Tool:
    """绑定当前实际使用的无参数工具，不提前引入 s02 的参数模型。

    输入：名称、说明、异步处理函数；输出：可交给循环的工具。
    流程：definition 给模型看 → execute_tool 检查空参数 → await execute。
    """

    name: str
    description: str
    execute: Callable[[dict[str, Any]], Awaitable[ToolResult]]

    def __post_init__(self) -> None:
        """在发请求前检查工具声明。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("工具名称不能为空")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("工具说明不能为空")
        if not callable(self.execute):
            raise TypeError("工具 execute 必须可调用")

    def definition(self) -> ToolDefinition:
        """声明只接收空对象；本章没有通用 JSON Schema 解释器。"""
        return ToolDefinition(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )


# s01 新增：这里只检查本章空参数契约，完整类型转换与嵌套校验在 s02 引入。
async def execute_tool(call: ToolCall, tools: Mapping[str, Tool]) -> ToolResultMessage:
    """查找并执行一次无参数工具，普通失败交回模型，原生取消继续传播。

    输入：调用与工具表；输出：保留原调用 ID 的结果。流程：查找 → 检查 → await → 包装。
    """
    try:
        tool = tools.get(call.name)
        if tool is None:
            raise ValueError(f"工具不存在：{call.name}")
        if not isinstance(call.arguments, dict) or call.arguments:
            raise ValueError("s01 工具只接受空参数对象；通用参数校验在 s02 加入")
        result = await tool.execute({})
        if not isinstance(result, ToolResult):
            raise TypeError("工具处理函数必须返回 ToolResult")
    except Exception as error:  # noqa: BLE001 - 普通工具错误回传模型，原生取消不在 Exception 内
        result = ToolResult(str(error), is_error=True)
    return ToolResultMessage(call.id, call.name, result.content, result.is_error)
