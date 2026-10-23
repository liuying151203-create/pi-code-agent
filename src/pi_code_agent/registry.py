"""s15/s18/s19：能力声明、状态查询、命令效果与启动参数运行时绑定。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal

import pi_code_agent.extension_base as previous

# s18 新增：命令与模型工具分别注册，共享当前 Agent 的命令表。
from pi_code_agent.commands import CommandHandler, CommandRegistry, ExtensionActions
from pi_code_agent.flags import FlagRegistry
from pi_code_agent.hooks import BeforeToolHook
from pi_code_agent.result import DispatchTool, ToolResult
from pi_code_agent.session import SessionManager

# s15 修改：扩展可返回正文字符串，也可返回携带状态的 ToolResult。
type ExtensionHandler = Callable[[dict[str, Any]], str | ToolResult]
# 来自 s14：保持；加载仍要求用户信任模块，失败停止，不新增自动发现机制。
load_extensions = previous.load_extensions


# s18 修改：在 s15 状态恢复与结果分发上增加共享命令表，工具注册继续复用 s14。
class ExtensionAPI(previous.ExtensionAPI):
    """在已有注册入口上增加结构化工具结果、状态查询和交互命令。

    输入：可选基础工具、后端、权限 Hook、会话和运行动作；省略时用于声明。
    输出：工具目录、Hooks、事件和命令注册入口，及带 details 的工具分发结果。
    流程：register 声明能力 → 应用参数 → Agent 绑定依赖 → session_start 恢复状态 → 执行。
    边界：扩展仍是可信宿主代码；权限、注册校验和事件订阅直接复用 s14。
    """

    def __init__(
        self,
        *,
        base_tools: list[dict[str, Any]] | None = None,
        base_dispatch: DispatchTool | None = None,
        permission_hook: BeforeToolHook | None = None,
        session: SessionManager | None = None,
        commands: CommandRegistry | None = None,
        actions: ExtensionActions | None = None,
    ) -> None:
        """保存会话与命令表，不在注册入口缓存或解释 Todo 状态。

        输入：可省略的工具依赖、会话和动作；输出：声明容器或已绑定注册层。
        流程：校验成组依赖 → 初始化能力表 → 创建参数表 → 后续 bind_runtime 绑定真实组件。
        """
        # s19 修改：声明期不需要 Provider/Session；运行能力稍后绑定，扩展模块只执行一次。
        supplied = (
            base_tools is not None,
            base_dispatch is not None,
            permission_hook is not None,
            session is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("基础工具、分发、权限和会话必须同时提供")
        super().__init__(
            base_tools=base_tools if base_tools is not None else [],
            base_dispatch=base_dispatch,
            permission_hook=permission_hook,
        )
        # s15 新增：会话是扩展恢复所需依赖，不通过全局变量或模型消息传入。
        self._session = session
        self.is_bound = all(supplied)
        self.flags = FlagRegistry()
        # s18 新增：Agent 显式注入；旧组装调用省略时创建独立表，保留 s15–s17 接口。
        self.commands = commands if commands is not None else CommandRegistry()
        # s18 新增：Pi runtime actions 的同步注入，旧教程仅注册工具时无需运行时动作。
        self.actions = actions

    # ===== s19 新增：声明完成后绑定真实会话与后端，保留已经注册的能力 =====
    @property
    def session(self) -> SessionManager:
        """声明期拒绝读取会话，启动后提供当前会话供扩展恢复状态。"""
        if self._session is None:
            raise RuntimeError("扩展运行时尚未绑定；会话恢复应在 session_start 中进行")
        return self._session

    def bind_runtime(
        self,
        *,
        base_tools: list[dict[str, Any]],
        base_dispatch: DispatchTool,
        permission_hook: BeforeToolHook,
        session: SessionManager,
        actions: ExtensionActions,
    ) -> None:
        """把一次性加载的扩展声明绑定到当前 Agent，不再次执行 register。

        输入：实际基础工具、权限、会话和动作；输出：无。
        流程：拒绝重复绑定 → 合并真实基础目录与扩展工具 → 保存运行依赖。
        边界：Hook、handler、事件和参数声明保持不变，不复用另一实例的已绑定容器。
        """
        if self.is_bound:
            raise ValueError("扩展容器已经绑定，不能复用于另一个 Agent")
        self.tools = deepcopy(base_tools) + self.tools
        self.base_names = {tool["name"] for tool in base_tools}
        self.base_dispatch = base_dispatch
        self.permission_hook = permission_hook
        self._session = session
        self.actions = actions
        self.is_bound = True

    def register_flag(
        self,
        name: str,
        *,
        type: Literal["boolean", "string"],
        description: str,
        default: bool | str | None = None,
    ) -> None:
        """对齐 Pi registerFlag，声明类型、帮助和默认值，不读取命令行。"""
        self.flags.register(name, type=type, description=description, default=default)

    def get_flag(self, name: str) -> bool | str | None:
        """对齐 Pi getFlag，启动解析后返回覆盖值，声明期只有默认值或 SDK 显式值。"""
        return self.flags.get(name)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """声明期拒绝工具执行；运行时继续复用真实基础工具权限入口。"""
        if not self.is_bound:
            raise RuntimeError("扩展运行时尚未绑定，不能执行工具")
        return super().call_tool(name, arguments)

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
