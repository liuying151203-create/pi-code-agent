# ===== s07 快照 =====
# 来自 s06：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s05：完整与流式模型协议；供应商 SDK 留在适配器中。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from .messages import AssistantMessage, Message


@dataclass(frozen=True)
class ToolDefinition:
    """提供给模型的工具名称、说明和 JSON Schema，不承担工具执行。"""

    name: str
    description: str
    parameters: dict[str, Any]


# 来自 s01：保持；参考 Pi 的模型调用注入，循环只依赖统一消息，不解析 HTTP 数据。
class Model(Protocol):
    """接收一次完整请求，异步返回归一化回答；客户端由创建者管理。"""

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AssistantMessage: ...


# ===== s05 新增 =====
# 来自 s05：保持；参考 Pi AssistantMessageEvent，只引入本章实际消费的文本事件和最终回答。
class StreamStart(TypedDict):
    """开始一条回答；partial 为 pending 快照。"""

    type: Literal["start"]
    partial: AssistantMessage


class StreamText(TypedDict):
    """携带一个文本块的边界或增量，以及该时刻的独立回答快照。"""

    type: Literal["text_start", "text_delta", "text_end"]
    content_index: int
    partial: AssistantMessage
    delta: NotRequired[str]
    content: NotRequired[str]


class StreamDone(TypedDict):
    """携带最终回答；工具只有在收到它并校验后才能执行。"""

    type: Literal["done"]
    message: AssistantMessage


type AssistantStreamEvent = StreamStart | StreamText | StreamDone


class StreamingModel(Protocol):
    """把一次模型请求转换为可关闭的异步生成器。

    输入：统一消息、工具定义和额度；输出：start、文本事件、done。
    流程：打开供应商流 → 逐段转为快照 → 最终回答 → 关闭连接。
    边界：异常继续抛出；消费者提前退出时应使用 aclosing 关闭生成器。
    """

    def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        max_tokens: int,
    ) -> AsyncGenerator[AssistantStreamEvent, None]: ...
