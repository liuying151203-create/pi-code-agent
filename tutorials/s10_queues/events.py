# ===== s10 快照 =====
# 来自 s09：保持；本章冻结实际使用的组件；新增行为见就近章节注释。
"""s04：运行事件与有序订阅，参考 Pi AgentEvent 和 Agent.subscribe。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from inspect import isawaitable
from typing import Any, Literal, TypedDict

from .messages import AssistantMessage, Message, ToolResultMessage
from .model import StreamText
from .tools import ToolResult


# ===== s04 新增 =====
# 来自 s05：保持；保留既有开始/结束事件，新增的文本更新事件在下方独立声明。
class LifecycleStart(TypedDict):
    """表示一次运行或一个模型回合开始。"""

    type: Literal["agent_start", "turn_start"]


class AgentEnd(TypedDict):
    """携带本次新增消息；运行结束不等于任务成功。"""

    type: Literal["agent_end"]
    messages: list[Message]


class TurnEnd(TypedDict):
    """携带本回合回答及按调用顺序保存的工具结果。"""

    type: Literal["turn_end"]
    message: AssistantMessage
    tool_results: list[ToolResultMessage]


class MessageEvent(TypedDict):
    """描述一条消息的开始或完成；增量变化使用 MessageUpdate。"""

    type: Literal["message_start", "message_end"]
    message: Message


# 来自 s05：保持；运行事件保留对应模型事件，界面既能拿增量，也能拿当前完整快照。
class MessageUpdate(TypedDict):
    """描述回答中的文本变化，不等于工具完成或回答完成。"""

    type: Literal["message_update"]
    message: AssistantMessage
    assistant_message_event: StreamText


class ToolExecutionStart(TypedDict):
    """描述一次工具尝试，发生在查找工具和参数校验之前。"""

    type: Literal["tool_execution_start"]
    tool_call_id: str
    tool_name: str
    args: Any


class ToolExecutionEnd(TypedDict):
    """描述尝试的最终结果；未执行也可以以错误结果结束。"""

    type: Literal["tool_execution_end"]
    tool_call_id: str
    tool_name: str
    result: ToolResult
    is_error: bool


type AgentEvent = (
    LifecycleStart
    | AgentEnd
    | TurnEnd
    | MessageEvent
    | MessageUpdate
    | ToolExecutionStart
    | ToolExecutionEnd
)
type EventListener = Callable[[AgentEvent], Awaitable[None] | None]


class EventListenerError(RuntimeError):
    """标记订阅器失败，避免把显示或记录失败伪装成模型、工具失败。"""


# 来自 s04：保持；移植 Pi 的按注册顺序等待语义，不创建后台任务或保存事件历史。
class EventBus:
    """分发本次运行事件，允许同步或异步订阅并返回取消订阅函数。

    输入：事件及监听函数；输出：emit 在所有订阅器完成后返回。
    流程：subscribe 注册 → emit 顺序调用并等待 → unsubscribe 移除。
    边界：不触发模型、不持久化；订阅失败直接中断调用，原生取消继续传播。
    """

    def __init__(self) -> None:
        """创建空的有序订阅表；注册本身不发送事件。"""
        self._listeners: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """注册监听器并返回可重复调用的取消函数；同一监听器不重复注册。"""
        if not callable(listener):
            raise TypeError("事件订阅器必须可调用")
        if listener not in self._listeners:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            """移除本监听器，已经移除时不做操作。"""
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def emit(self, event: AgentEvent) -> None:
        """发送独立观察副本，按顺序等待每个订阅器；订阅变更从下个事件生效。"""
        # s04 调整：Pi 直接遍历 Set；Python 固定本次名单，并隔离观察副本，避免改写工具参数。
        for listener in tuple(self._listeners):
            try:
                result = listener(deepcopy(event))
                if isawaitable(result):
                    await result
            except Exception as error:
                raise EventListenerError(f"事件 {event['type']} 的订阅器失败：{error}") from error
