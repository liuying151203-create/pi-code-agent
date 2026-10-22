"""s15 修改：复用 s14 注册机制，接入结构化结果及会话状态查询。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tutorials.s04_hooks.code import BeforeToolHook
from tutorials.s14_extensions import registry as previous
from tutorials.s15_stateful_extensions.result import DispatchTool, ToolResult
from tutorials.s15_stateful_extensions.session import SessionManager

# s15 修改：扩展可返回正文字符串，也可返回携带状态的 ToolResult。
type ExtensionHandler = Callable[[dict[str, Any]], str | ToolResult]
# 来自 s14：保持；加载仍要求用户信任模块，失败停止，不新增自动发现机制。
load_extensions = previous.load_extensions


# s15 修改：继承 s14 的注册容器，仅扩充当前状态恢复与结果分发两个职责。
class ExtensionAPI(previous.ExtensionAPI):
    """在 s14 注册入口上增加结构化结果和状态恢复查询。

    输入：基础工具、后端、权限 Hook，以及当前会话。
    输出：沿用的工具目录、Hooks、事件入口，及带 details 的工具分发结果。
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
    ) -> None:
        """保存会话依赖，不在注册入口缓存或解释 Todo 状态。

        输入：s14 原有依赖及本章会话；输出：独立注册层。
        流程：复用 s14 初始化 → 保存 session，供 get_tool_details 显式查询。
        """
        super().__init__(
            base_tools=base_tools, base_dispatch=base_dispatch, permission_hook=permission_hook
        )
        # s15 新增：会话是扩展恢复所需依赖，不通过全局变量或模型消息传入。
        self.session = session

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
