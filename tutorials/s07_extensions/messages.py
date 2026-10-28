# ===== s07 快照 =====
# 来自 s06：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
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


# 来自 s03：保持；回补推理内容与签名保存，避免后续章节重现同一协议错误。
# s03 新增：兼容真实模型返回的推理块，保留回传签名，与终端展示的 Text 区分。
@dataclass(frozen=True)
class Thinking:
    """保存推理文本及回传数据，不承担终端展示或工具执行。

    输入：推理文本、原始签名与脱敏标记；输出：供 Provider 下次请求使用的内容块。
    流程：接收响应 → 保存在回答中 → 原样回传有签名内容；脱敏块只保存不透明数据。
    """

    text: str
    signature: str = field(default="", repr=False)
    redacted: bool = False

    def __post_init__(self) -> None:
        """检查字段类型，避免把未知对象当作可回传的协议内容。"""
        if not isinstance(self.text, str) or not isinstance(self.signature, str):
            raise TypeError("推理文本和签名必须是字符串")
        if type(self.redacted) is not bool:
            raise TypeError("推理脱敏标记必须是布尔值")


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

    输入：文本/推理/工具调用块、归一化停止状态；输出：不可变回答。
    流程：模型创建 → 循环记录 → 按状态执行工具或结束。
    边界：error/aborted 也是实际结果，不能仅凭有文本就视作成功。
    """

    # 来自 s03：保持；回答同时保存文本、推理与工具请求。
    content: tuple[Text | Thinking | ToolCall, ...]
    # 来自 s05：保持；pending 仅表示流中的回答快照，不能作为最终回答执行工具。
    stop_reason: Literal["stop", "tool_use", "length", "error", "aborted", "pending"] = "stop"
    usage: Usage | None = None
    # 来自 s03：保持；保留供应商的失败原因，与模型正文分开，避免伪造回答文本。
    error_message: str | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        """固定内容快照，拒绝未支持的块和停止状态。"""
        content = tuple(self.content)
        if any(not isinstance(block, (Text, Thinking, ToolCall)) for block in content):
            raise TypeError("当前只支持文本、推理和工具调用块")
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


type Message = UserMessage | AssistantMessage | ToolResultMessage
