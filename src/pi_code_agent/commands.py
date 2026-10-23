"""s18：命令注册、上下文和效果接口，模型运行仍由 Agent 负责。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pi_code_agent.events import AgentEvent, EventSink

if TYPE_CHECKING:
    from pi_code_agent.session import SessionManager


# ===== s18 新增：handler 选择效果，返回值只表示命令执行状态 =====
@dataclass(frozen=True)
class CommandResult:
    """表示命令处理是否失败，不携带强制展示或模型正文。

    输入：错误标记和可选错误说明；输出：SDK 命令执行状态。
    流程：Agent 调用 handler → 保存成功或失败事实 → 返回状态。
    边界：模型回合失败仍抛异常；已完成命令的状态不冒充模型任务结果。
    """

    is_error: bool = False
    error: str | None = None


class ExtensionActions(Protocol):
    """定义扩展使用的实际运行能力，由 Agent 注入。

    输入：状态数据、消息及展示/触发选项；输出：无。
    流程：ExtensionAPI 转交 → Agent 检查时序并保存 → 按需进入原有循环。
    边界：私有状态可独立保存；消息支持空闲或受控扩展回调，不预留忙时投递选项。
    """

    def append_entry(self, custom_type: str, data: Any = None) -> None: ...

    def send_message(
        self,
        content: str,
        *,
        custom_type: str = "extension",
        display: bool = True,
        details: Any = None,
        trigger_turn: bool = False,
    ) -> None: ...

    def send_user_message(self, content: str) -> None: ...


# s18 新增：对齐 Pi ctx.ui.notify，用同一事件链供 CLI 与 SDK 消费。
@dataclass(frozen=True)
class CommandUI:
    """发送界面通知，不写 Session 或请求模型。

    输入：事件出口和通知正文；输出：notification 事件。
    流程：校验正文和级别 → 发送事件 → 终端或 SDK 观察者消费。
    """

    emit: EventSink

    def notify(self, message: str, level: str = "info") -> None:
        """发送一条用户通知，级别为 info、warning 或 error。"""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("通知正文必须是非空字符串")
        if level not in {"info", "warning", "error"}:
            raise ValueError("通知级别必须为 info、warning 或 error")
        self.emit(AgentEvent("notification", {"message": message, "level": level}))


@dataclass(frozen=True)
class CommandContext:
    """提供命令本次使用的界面和会话依赖。

    输入：CommandUI 与当前 SessionManager；输出：handler(args, ctx) 的上下文。
    流程：Agent 创建 → handler 使用 ui 或读取 session；消息写入通过 ExtensionAPI。
    边界：不提供尚未实现的切换会话、取消或异步 UI 方法。
    """

    ui: CommandUI
    session: SessionManager


type CommandHandler = Callable[[str, CommandContext], None]


@dataclass(frozen=True)
class RegisteredCommand:
    """关联命令名称、帮助说明和 handler，供注册表查找与展示。"""

    name: str
    description: str
    handler: CommandHandler


# s18 新增：参考 Pi registerCommand，移植为同步 handler，重复名称明确拒绝。
class CommandRegistry:
    """收集实例命令并生成与真实能力一致的帮助。

    输入：名称、说明和 handler；输出：命令定义或帮助通知。
    流程：注册校验 → 名称查找 → Agent 调用；帮助枚举同一张表。
    边界：不执行模型，不把命令结果默认发送到上下文。
    """

    def __init__(self) -> None:
        self._commands: dict[str, RegisteredCommand] = {}

    def register(self, name: str, handler: CommandHandler, *, description: str) -> None:
        """校验名称、说明及函数后注册；不执行 handler，重名直接报错。"""
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("命令名称必须为小写字母开头的标识符，长度不超过 64，不带 /")
        if name in self._commands:
            raise ValueError(f"命令名称重复：/{name}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("命令说明必须是非空字符串")
        if not callable(handler):
            raise TypeError("命令 handler 必须可调用")
        self._commands[name] = RegisteredCommand(name, description.strip(), handler)

    def get(self, name: str) -> RegisteredCommand | None:
        """按原名查找命令，不自动纠错。"""
        return self._commands.get(name)

    def help(self, args: str, ctx: CommandContext) -> None:
        """把当前命令帮助作为通知展示，不发送模型消息。

        输入：空参数与命令上下文；输出：无，发送一条通知。
        流程：拒绝参数 → 按名称排序 → 格式化 → ctx.ui.notify。
        """
        if args:
            raise ValueError("/help 不接受参数")
        commands = sorted(self._commands.values(), key=lambda command: command.name)
        lines = ["可用命令："]
        lines.extend(f"  /{command.name}    {command.description}" for command in commands)
        ctx.ui.notify("\n".join(lines))
