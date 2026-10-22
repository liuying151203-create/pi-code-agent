"""s14 新增：把工具、Hook 和事件注册集中到受信任的扩展入口。"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any, get_args

from tutorials.s04_hooks.code import AfterToolHook, BeforeToolHook, Hooks
from tutorials.s11_runtime_observability.code import (
    AgentEvent,
    AgentEventType,
    DispatchTool,
    EventSink,
)

# 来自 s12：保持；全体内置名称均保留，不因启动模式筛选而允许被扩展覆盖。
from tutorials.s12_skill_loading.code import TOOLS

# 来自 s13-A：保持；致命后端错误不能伪装成普通工具结果后继续。
from tutorials.s13_a_sandbox_copy.sandbox import SandboxError

# s14 新增：扩展 handler 只接收工具参数；需要工作区操作时显式调用 api.call_tool。
type ExtensionHandler = Callable[[dict[str, Any]], str]


# s14 新增：参考 Pi 的注册 API，复用现有 Hooks 和 Event，不另建生命周期系统。
class ExtensionAPI:
    """收集扩展能力，并连接已有工具权限与执行后端。

    输入：当前模式开放的基础工具、实际分发函数和基础工具权限 Hook。
    输出：模型工具目录、扩展 Hooks、dispatch 分发入口及事件消费者 handle_event。
    流程：load_extensions 调用 register → 扩展注册能力 → main 注入循环和事件总线。
    边界：这是受信任的宿主 Python 扩展，不是沙箱。call_tool 只调用基础工具，
    每次先检查真实工具名和参数；扩展不能通过该入口调用另一个扩展形成递归。
    """

    def __init__(
        self,
        *,
        base_tools: list[dict[str, Any]],
        base_dispatch: DispatchTool,
        permission_hook: BeforeToolHook,
    ) -> None:
        """保存执行依赖，并为本次启动创建独立注册表。

        输入：基础工具目录、分发函数、权限检查；输出：尚未加载扩展的实例。
        流程：复制目录 → 保存执行依赖 → 创建空 handler、Hook 和事件集合。
        不修改 s12 的全局 TOOLS，也不恢复历史会话中的授权或 Python 对象。
        """
        self.tools = deepcopy(base_tools)
        self.base_dispatch = base_dispatch
        self.permission_hook = permission_hook
        self.base_names = {tool["name"] for tool in base_tools}
        self.handlers: dict[str, ExtensionHandler] = {}
        self.hooks = Hooks()
        self.listeners: dict[str, list[EventSink]] = {}

    # s14 新增：定义交给模型，handler 留在宿主；重复名称不能覆盖原有能力。
    def register_tool(self, definition: dict[str, Any], handler: ExtensionHandler) -> None:
        """将工具定义和执行函数一起注册，避免目录与分发器分离。

        输入：含 name、description、input_schema 的定义，以及接收参数字典的函数。
        输出：无；定义加入 tools，函数按名称加入 handlers。
        流程：校验名称、重复与基本结构 → 复制定义 → 保存 handler。
        边界：这里只校验注册结构，不实现通用 JSON Schema 验证；参数由 handler 校验。
        """
        if not isinstance(definition, dict):
            raise TypeError("扩展工具定义必须是字典")
        name = definition.get("name")
        description = definition.get("description")
        schema = definition.get("input_schema")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            raise ValueError("扩展工具名称必须是字母开头的标识符，长度不超过 64")
        # s14 新增：保护全部内置名称，不能借只读模式未显示某工具而覆盖它。
        reserved_names = {tool["name"] for tool in TOOLS}
        if name in reserved_names or name in self.handlers or name in self.base_names:
            raise ValueError(f"工具名称重复或属于内置工具：{name}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"扩展工具缺少描述：{name}")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"扩展工具 input_schema 必须声明 object：{name}")
        if not callable(handler):
            raise TypeError(f"扩展工具 handler 必须可调用：{name}")
        self.tools.append(deepcopy(definition))
        self.handlers[name] = handler

    # s14 新增：注册只收集回调，实际阻断仍由 s04 执行，不在注册时运行工具。
    def before_tool_call(self, hook: BeforeToolHook) -> None:
        """注册前置 Hook；执行顺序仍由 s04 的 Hooks 决定。"""
        if not callable(hook):
            raise TypeError("前置 Hook 必须可调用")
        self.hooks.before_tool_call.append(hook)

    # s14 新增：后置回调仍进入 s04 的同一结果处理链。
    def after_tool_call(self, hook: AfterToolHook) -> None:
        """注册结果处理 Hook；每个返回值交给下一个 Hook。"""
        if not callable(hook):
            raise TypeError("后置 Hook 必须可调用")
        self.hooks.after_tool_call.append(hook)

    # s14 新增：收集已有事件的订阅，不修改循环的事件发送位置。
    def on(self, event_type: str, listener: EventSink) -> None:
        """订阅一种已有事件，不新增另一套事件名称。

        输入：s11 的事件类型及接收 AgentEvent 的函数；输出：无。
        流程：检查名称和函数 → 按事件类型保存；handle_event 在运行时通知对应函数。
        """
        # s14 新增：Python 的 type 别名包装了 Literal，读取实际取值后校验订阅名。
        if event_type not in get_args(AgentEventType.__value__):
            raise ValueError(f"未知事件类型：{event_type}")
        if not callable(listener):
            raise TypeError("事件观察者必须可调用")
        self.listeners.setdefault(event_type, []).append(listener)

    def handle_event(self, event: AgentEvent) -> None:
        """将 EventDispatcher 的事件交给匹配的扩展观察者。

        输入：已有运行时事件；输出：无，观察者处理数据。
        流程：按类型查找 → 按注册顺序通知；与 s11 一样，观察者异常直接暴露。
        Hook 控制执行，Event 只报告事实，二者不混用。
        """
        for listener in tuple(self.listeners.get(event.type, ())):
            listener(event)

    # s14 新增：检查委托后的真实工具名，不能只审批 read_preview 这样的包装名称。
    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """通过已有权限入口调用一个基础工具，不调用扩展 handler。

        输入：基础工具真实名称和参数；输出：拒绝原因或后端结果文本。
        流程：校验参数 → 权限 Hook → 检查开放目录 → 实际后端。
        此入口供基础分发和扩展委托共同使用；不捕获后端致命异常，不自动换后端。
        """
        if not isinstance(arguments, dict):
            return "Error: 工具参数必须是对象"
        reason = self.permission_hook(name, arguments)
        if reason is not None:
            return reason
        if name not in self.base_names:
            return f"Error: 当前模式未开放基础工具：{name}"
        return self.base_dispatch(name, arguments)

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """区分基础工具与扩展工具，但不改变核心循环。

        输入：模型工具名和参数；输出：结果文本，参数错误也作为工具结果返回。
        流程：基础工具走 call_tool → 扩展名称查 handler → 校验返回类型。
        外层 s04 execute_tool 已负责扩展 Hooks；基础权限在 call_tool 中再次按真实操作检查。
        """
        if not isinstance(arguments, dict):
            return "Error: 工具参数必须是对象"
        handler = self.handlers.get(name)
        if handler is None:
            return self.call_tool(name, arguments)
        try:
            result = handler(arguments)
            if not isinstance(result, str):
                raise TypeError("扩展工具必须返回字符串")
            return result
        except SandboxError:
            raise
        except Exception as error:  # noqa: BLE001 - 普通扩展错误回传模型，致命后端错误除外
            return f"Error: 扩展工具 {name} 执行失败：{error}"


# s14 新增：只加载启动时明确选择的模块；不扫描目标仓库并执行其中的 Python。
def load_extensions(api: ExtensionAPI, module_names: Sequence[str]) -> tuple[str, ...]:
    """加载受信任 Python 模块，并调用其 register(api)。

    输入：本次启动的 ExtensionAPI，以及模块名列表；输出：成功注册的模块名元组。
    流程：先拒绝重复和非法模块名 → import_module → 检查 register → 顺序注册。
    边界：导入会执行 Python；模块必须由用户信任。任一注册失败就中止启动，
    不带半注册状态进入交互，也不尝试替代模块；不提供热重载或历史自动加载。
    """
    if len(set(module_names)) != len(module_names):
        raise ValueError("扩展模块不能重复加载")
    for name in module_names:
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", name):
            raise ValueError(f"扩展应使用 Python 模块名，不是文件路径：{name}")
    for name in module_names:
        try:
            module = importlib.import_module(name)
            register = getattr(module, "register", None)
            if not callable(register):
                raise TypeError("模块必须提供 register(api)")
            register(api)
        except Exception as error:  # 注册失败必须阻止整个入口启动
            raise RuntimeError(f"扩展加载失败：{name}：{error}") from error
    return tuple(module_names)
