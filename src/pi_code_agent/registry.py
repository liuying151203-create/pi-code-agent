"""s15/s18：工具状态恢复、命令注册与显式效果转发。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pi_code_agent.extension_base as previous

# s18 新增：命令单独注册，效果通过已绑定的 Agent 执行。
from pi_code_agent.commands import CommandHandler, CommandRegistry, ExtensionActions
from pi_code_agent.hooks import BeforeToolHook
from pi_code_agent.result import DispatchTool, ToolResult
from pi_code_agent.session import SessionManager

# s15 修改：扩展可返回正文字符串，也可返回携带状态的 ToolResult。
type ExtensionHandler = Callable[[dict[str, Any]], str | ToolResult]
# 来自 s14：保持；加载仍要求用户信任模块，失败停止，不新增自动发现机制。
load_extensions = previous.load_extensions


# s18 修改：沿用工具状态查询，增加命令表及效果转发，不改变工具分发。
class ExtensionAPI(previous.ExtensionAPI):
    """在 s14 注册入口上增加状态恢复、交互命令和显式效果。

    输入：基础工具、后端、权限 Hook、当前会话，以及可选命令表和运行时动作。
    输出：工具目录、Hooks、事件和命令注册入口，以及带 details 的工具分发结果。
    流程：main 打开会话 → register 查询旧状态 → 注册工具 → 循环执行并保存新结果。
    边界：扩展仍是可信宿主代码；权限、注册校验和事件订阅直接复用 s14。
    """

    def __init__(
        self,
        *,
        base_tools: list[dict[str, Any]],
        base_dispatch: DispatchTool,
        permission_hook: BeforeToolHook,
        session: SessionManager,
        # s18 新增：共享命令注册和效果接口，旧章节只使用工具时可省略。
        commands: CommandRegistry | None = None,
        actions: ExtensionActions | None = None,
    ) -> None:
        """保存会话、命令表与动作，不在注册入口解释 Todo 状态或运行模型。

        输入：工具依赖、会话，以及可选命令表和动作；输出：独立注册层。
        流程：复用 s14 初始化 → 保存会话 → 保存命令表和效果转发依赖。
        """
        super().__init__(
            base_tools=base_tools, base_dispatch=base_dispatch, permission_hook=permission_hook
        )
        # s15 新增：会话是扩展恢复所需依赖，不通过全局变量或模型消息传入。
        self.session = session
        # s18 新增：与 Agent 共享命令表和动作，不在注册层重复调度模型。
        self.commands = commands if commands is not None else CommandRegistry()
        self.actions = actions

    # s18 新增：参考 Pi 的扩展命令注册，本章不注入 UI、会话切换或异步上下文。
    def register_command(self, name: str, handler: CommandHandler, *, description: str) -> None:
        """向当前 Agent 注册接收 args、ctx 的交互命令。

        输入：不带 / 的名称、同步 handler 和帮助说明；输出：无。
        流程：委托共享 CommandRegistry 校验注册；由 Agent.execute_command 执行和保存。
        边界：可信宿主回调，不自动进入工具 Hook，不把扩展注册当作 OS 隔离。
        """
        if name in {"help", "stats"}:
            raise ValueError(f"命令名称重复或属于内置命令：/{name}")
        self.commands.register(name, handler, description=description)

    # s18 新增：对齐 Pi appendEntry/sendMessage/sendUserMessage，具体保存和调度由 Agent 承担。
    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """保存扩展 JSON 状态，不产生模型消息。"""
        if self.actions is None:
            raise RuntimeError("当前组装未绑定扩展运行能力")
        self.actions.append_entry(custom_type, data)

    def send_message(
        self,
        content: str,
        *,
        custom_type: str = "extension",
        display: bool = True,
        details: Any = None,
        trigger_turn: bool = False,
    ) -> None:
        """发送模型可见消息，display 控制界面，trigger_turn 控制是否继续模型。

        输入：文本、类型、展示/触发开关和元数据；输出：无。
        流程：转交 Agent → 先保存 → 展示或等待后续回合；命令内触发延后到 handler 结束。
        """
        if self.actions is None:
            raise RuntimeError("当前组装未绑定扩展运行能力")
        self.actions.send_message(
            content,
            custom_type=custom_type,
            display=display,
            details=details,
            trigger_turn=trigger_turn,
        )

    def send_user_message(self, content: str) -> None:
        """发送真正的用户文本并请求一轮模型；命令内由外层在结束后执行。"""
        if self.actions is None:
            raise RuntimeError("当前组装未绑定扩展运行能力")
        self.actions.send_user_message(content)

    # 来自 s14：保持；只扩大类型说明，名称和定义校验仍使用原有方法。
    def register_tool(self, definition: dict[str, Any], handler: ExtensionHandler) -> None:
        """注册字符串或结构化结果 handler，不改变 s14 的重复名称规则。"""
        super().register_tool(definition, handler)

    def get_tool_details(self, name: str) -> dict[str, Any] | None:
        """查询工具最后保存的状态，供扩展初始化使用。

        输入：工具名称；输出：details 副本或 None。
        流程：委托会话读取完整日志；不从模型上下文猜测状态，不触发工具调用。
        """
        # s15 新增：把已注入的会话查询提供给 register，不让扩展解析模型正文。
        return self.session.latest_tool_details(name)

    # s15 修改：s14 只接受字符串，本章也接受独立的状态快照。
    def dispatch(self, name: str, arguments: dict[str, Any]) -> str | ToolResult:
        """分发扩展结果，同时保留原有基础工具权限。

        输入：工具名和参数；输出：字符串错误/基础结果，或扩展 ToolResult。
        流程：基础工具走 call_tool → 扩展 handler → 检查结果类型。
        普通扩展异常变为工具错误，致命后端异常仍让循环停止。
        """
        if not isinstance(arguments, dict):
            return "Error: 工具参数必须是对象"
        handler = self.handlers.get(name)
        if handler is None:
            return self.call_tool(name, arguments)
        try:
            result = handler(arguments)
            if not isinstance(result, (str, ToolResult)):
                raise TypeError("扩展工具必须返回字符串或 ToolResult")
            return result
        except previous.SandboxError:
            raise
        except Exception as error:  # noqa: BLE001 - 普通扩展异常仍作为工具结果返回
            return f"Error: 扩展工具 {name} 执行失败：{error}"
