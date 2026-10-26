# ===== s01 新增 =====
# s01 新增；按 s01 文本/工具消息阶段冻结，不包含 s03 才使用的用量和供应商失败原因。
"""s01：供应商无关的文本、工具调用和工具结果；s03 补充模型用量。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal


# s01 新增：用户文本和工具结果是不同角色，不借用任何供应商的协议字段。
@dataclass(frozen=True)
class UserMessage:
    """保存用户输入；由循环传给模型，序列化交给 s03 的 Provider。"""

    content: str
    role: Literal["user"] = field(default="user", init=False)


@dataclass(frozen=True)
class Text:
    """保存模型回答中的一个文本块。"""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """保存模型提出的工具请求；参数保持原样，实际执行前由 s02 校验。"""

    id: str
    name: str
    arguments: Any

    def __post_init__(self) -> None:
        """检查调用标识，复制参数以隔离模型响应与后续校验。"""
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("工具调用必须有非空 id")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("工具调用必须有非空名称")
        object.__setattr__(self, "arguments", deepcopy(self.arguments))


@dataclass(frozen=True)
class AssistantMessage:
    """保存一轮完整模型回答，包括停止状态。

    输入：文本/工具调用块、归一化停止状态；输出：不可变回答。
    流程：模型创建 → 循环记录 → 按状态执行工具或结束。
    边界：error/aborted 也是实际结果，不能仅凭有文本就视作成功。
    """

    content: tuple[Text | ToolCall, ...]
    stop_reason: Literal["stop", "tool_use", "length", "error", "aborted"] = "stop"
    role: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        """固定内容快照，拒绝未支持的块和停止状态。"""
        content = tuple(self.content)
        if any(not isinstance(block, (Text, ToolCall)) for block in content):
            raise TypeError("当前只支持文本和工具调用块")
        if self.stop_reason not in {"stop", "tool_use", "length", "error", "aborted"}:
            raise ValueError("未知停止状态")
        object.__setattr__(self, "content", content)


@dataclass(frozen=True)
class ToolResultMessage:
    """将工具正文及独立错误标记与原调用 ID 关联，作为独立消息交还模型。"""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    role: Literal["tool_result"] = field(default="tool_result", init=False)


type Message = UserMessage | AssistantMessage | ToolResultMessage
