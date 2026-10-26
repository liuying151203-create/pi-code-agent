# ===== 来自 s01：保持 =====
# 来自 s01：保持；保存本章阶段的组件实现；相对导入只引用本章，不读取正式包或其他章节。
"""s01：循环依赖的最小异步模型协议，不导入供应商 SDK。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

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
