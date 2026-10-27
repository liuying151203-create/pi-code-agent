# ===== s12 快照 =====
# 来自 s11：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s01：供应商无关的文本、工具调用和工具结果；s03 补充模型用量。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal


# 来自 s01：保持；用户文本和工具结果是不同角色，不借用任何供应商的协议字段。
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


# 来自 s03：保持；Provider 归一化输入/输出用量，供当前命令行展示，暂不计算费用。
@dataclass(frozen=True)
class Usage:
    """描述一次模型请求的输入和输出 Token 数量。"""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class AssistantMessage:
    """保存最终模型回答或 pending 流式快照，包括状态和可选用量。

    输入：文本/工具调用块、归一化停止状态；输出：不可变回答。
    流程：模型创建 → 循环记录 → 按状态执行工具或结束。
    边界：error/aborted 也是实际结果，不能仅凭有文本就视作成功。
    """

    content: tuple[Text | ToolCall, ...]
    # 来自 s05：保持；pending 仅表示流中的回答快照，不能作为最终回答执行工具。
    stop_reason: Literal["stop", "tool_use", "length", "error", "aborted", "pending"] = "stop"
    usage: Usage | None = None
    # 来自 s03：保持；保留供应商的失败原因，与模型正文分开，避免伪造回答文本。
    error_message: str | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        """固定内容快照，拒绝未支持的块和停止状态。"""
        content = tuple(self.content)
        if any(not isinstance(block, (Text, ToolCall)) for block in content):
            raise TypeError("当前只支持文本和工具调用块")
        if self.stop_reason not in {"stop", "tool_use", "length", "error", "aborted", "pending"}:
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


# 来自 s09：保持；显示选择和模型可见性分开，自定义消息默认进入模型上下文。
@dataclass(frozen=True)
class CustomMessage:
    """扩展投递的文本，display 只控制界面；状态记录使用独立 custom 条目。"""

    custom_type: str
    content: str
    display: bool = True
    role: Literal["custom"] = field(default="custom", init=False)

    def __post_init__(self):
        if (
            not isinstance(self.custom_type, str)
            or not self.custom_type
            or not isinstance(self.content, str)
            or type(self.display) is not bool
        ):
            raise ValueError("自定义消息需要类型、文本与布尔 display")


type Message = UserMessage | AssistantMessage | ToolResultMessage | CustomMessage


def convert_to_llm(messages: list[Message]) -> list[Message]:
    """参考 Pi convertToLlm：自定义消息转换为用户文本，展示标记不影响模型可见性。"""
    return [UserMessage(m.content) if isinstance(m, CustomMessage) else m for m in messages]
